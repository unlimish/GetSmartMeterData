#!/usr/bin/python3
# -*- coding: utf-8 -*-

"""
Get SmartMeter data via Wi-SUN Profile for ECHONET Lite
and POST to Home Assistant webhook.

Original: tonasuzuki (2024/03/03)
Refactored for robustness and clearer value handling.
"""

import requests
import logging
import sys
import serial
import signal
import time
import atexit
from typing import Optional

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------

# Bルートサービス認証情報
B_ROUTE_ID = '0123456789abcdef0123456789abcdef'  # 32文字の認証ID
B_ROUTE_PW = '0123456789ab'                      # 12文字のパスワード

# シリアルポート設定
SERIAL_PORT = '/dev/ttyS1'
SERIAL_SPEED = 115200
SERIAL_TIMEOUT_SEC = 5
COMMAND_TIMEOUT_SEC = 30

# データ取得間隔
UPDATE_DATA_TIME_SEC = 120

# 接続リトライ
CONNECT_RETRY_COUNT = 4
EchonetCommand_RETRIES = 3

# HomeAssistant Webhook URL
HA_URL = 'http://homeassistant.local:8123/api/webhook/echonet-aki-wi-sun'

# バリデーション閾値
MAX_MEASURED_POWER_W = 6000      # 瞬時電力の異常値上限（W）
MAX_INTEGRATED_POWER_KWH = 1_000_000  # 積算電力量の異常値上限（kWh相当）

# 積算電力量を HA と同じスケールに合わせる係数
# HA 側のテンプレートから `* 0.1` を外し、このスクリプトで `* 0.1` を済ませる
INTEGRATED_POWER_DISPLAY_SCALE = 0.1

# ロギング（実運用時は INFO、デバッグ時は DEBUG）
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s:%(name)s - %(message)s"
)


# ---------------------------------------------------------------------------
# ECHONET Lite 通信クラス
# ---------------------------------------------------------------------------

class CommEchoNet:
    """Wi-SUN モジュールを介して ECHONET Lite デバイスと通信する。"""

    # ECHONET Lite コマンドヘッダー
    ECHONET_COMMAND_HEADER = b'\x10\x81\x00\x01\x05\xFF\x01\x02\x88\x01\x62\x01'

    # 積算電力量の単位テーブル（E1 プロパティの EDT → kWh 換算係数）
    INTEGRATED_POWER_UNIT_TABLE = {
        0x00: 1.0,
        0x01: 0.1,
        0x02: 0.01,
        0x03: 0.001,
        0x04: 0.0001,
        0x0A: 10.0,
        0x0B: 100.0,
        0x0C: 1000.0,
        0x0D: 10000.0,
    }

    def __init__(self) -> None:
        self._ser: Optional[serial.Serial] = None
        self.local_ip_addr: Optional[str] = None
        self.scanned_desc: dict = {}
        self.open_serial()

    def __del__(self) -> None:
        self.terminate_communication()
        self.close_serial()

    # ---- シリアル ----

    def open_serial(self) -> None:
        self._ser = serial.Serial(SERIAL_PORT, SERIAL_SPEED)
        self._ser.timeout = SERIAL_TIMEOUT_SEC

    def close_serial(self) -> None:
        if self._ser and self._ser.is_open:
            self._ser.close()

    # ---- 低レベルコマンド ----

    def _read_line(self) -> str:
        """シリアルから1行読み込む。"""
        if not self._ser:
            return ''
        raw = self._ser.readline()
        return raw.decode('utf-8', errors='replace').rstrip('\r\n')

    def send_command(self, command: str, raw_data: Optional[bytes] = None) -> bool:
        """OK/FAIL が返るコマンドを送信する。"""
        if raw_data is None:
            data = (command + "\r\n").encode('utf-8')
        else:
            data = command.encode('utf-8') + raw_data

        self._ser.write(data)
        logging.debug(data)

        deadline = time.time() + COMMAND_TIMEOUT_SEC
        while time.time() < deadline:
            line = self._read_line()
            logging.debug(line)
            if line.startswith("OK"):
                return True
            if line.startswith("FAIL"):
                return False
        return False

    def get_property(self, command: str) -> str:
        """応答文字列が直接返るコマンドを送信する。"""
        self._ser.write((command + "\r\n").encode('utf-8'))
        self._ser.readline()  # エコーバック
        return self._read_line()

    def check_command_event(self) -> int:
        """EVENT が返るまで待ち、イベントコードを返す。"""
        deadline = time.time() + COMMAND_TIMEOUT_SEC
        while time.time() < deadline:
            line = self._read_line()
            logging.debug(line)
            if line.startswith("EVENT"):
                return int(line[6:8], 16)
        return -1

    def terminate_communication(self) -> int:
        logging.info('終了コマンド送信')
        self.send_command("SKTERM")
        return self.check_command_event()

    # ---- Bルート / PANA 接続 ----

    def set_id(self, b_route_id: str, b_route_pw: str) -> bool:
        logging.info('Bルート認証ID設定')
        if not self.send_command("SKSETRBID " + b_route_id):
            return False
        logging.info('Bルートパスワード設定')
        return self.send_command("SKSETPWD C " + b_route_pw)

    def scan_device(self) -> bool:
        logging.info('デバイススキャン')
        if not self.send_command("SKSCAN 2 FFFFFFFF 6 0"):
            return False

        in_description = False
        while True:
            line = self._read_line()
            logging.debug(line)

            if line.startswith("EVENT 22"):
                logging.info('アクティブスキャン完了')
                break
            elif line.startswith("EPANDESC"):
                in_description = True
            elif in_description and (':' in line):
                key, value = [x.strip() for x in line.split(':', 1)]
                self.scanned_desc[key] = value
            else:
                in_description = False

        return self.scanned_desc.get('Addr') is not None

    def set_device_param(self) -> bool:
        channel = self.scanned_desc.get('Channel')
        pan_id = self.scanned_desc.get('Pan ID')
        addr = self.scanned_desc.get('Addr')

        if not all([channel, pan_id, addr]):
            return False

        logging.info('Channel設定')
        if not self.send_command("SKSREG S2 " + channel):
            return False

        logging.info('PanID設定')
        if not self.send_command("SKSREG S3 " + pan_id):
            return False

        logging.info('MACアドレスをIPv6リンクローカルアドレスに変換')
        self.local_ip_addr = self.get_property("SKLL64 " + addr)
        return bool(self.local_ip_addr)

    def connect_device(self) -> bool:
        logging.info('PANA接続シーケンス')
        if not self.local_ip_addr:
            return False

        if not self.send_command("SKJOIN " + self.local_ip_addr):
            return False

        deadline = time.time() + COMMAND_TIMEOUT_SEC
        connected = False
        while time.time() < deadline:
            line = self._read_line()
            logging.debug(line)
            if line.startswith("EVENT 24"):
                logging.info('PANA 接続失敗')
                return False
            if line.startswith("EVENT 25"):
                logging.info('PANA 接続成功')
                connected = True
                break

        # EVENT 25 の後に届く ERXUDP を読み捨てる
        self._read_line()
        return connected

    def init_connection(self) -> bool:
        if len(B_ROUTE_ID) != 32 or len(B_ROUTE_PW) != 12:
            logging.error('B_ROUTE_IDまたはB_ROUTE_PWの桁数が正しくありません')
            return False

        if not self.set_id(B_ROUTE_ID.upper(), B_ROUTE_PW.upper()):
            return False

        for attempt in range(CONNECT_RETRY_COUNT, 0, -1):
            if self.scan_device():
                break
            logging.warning(f'デバイススキャンリトライ残り {attempt - 1} 回')
        else:
            logging.error('ECHONetデバイスが見つかりません')
            return False

        if not self.set_device_param():
            logging.error('接続先を設定できませんでした')
            return False

        if not self.connect_device():
            logging.error('接続先に接続できませんでした')
            return False

        return True

    # ---- ECHONET Lite プロパティ取得 ----

    def send_echonet_command(
        self,
        epc: bytes,
        max_retries: int = EchonetCommand_RETRIES,
    ) -> Optional[int]:
        """指定した EPC のプロパティ値を取得する。失敗時は None。"""
        echonet_command = self.ECHONET_COMMAND_HEADER + epc + b'\x00'
        command = "SKSENDTO 1 {0} 0E1A 1 0 {1:04X} ".format(
            self.local_ip_addr, len(echonet_command)
        )

        for attempt in range(1, max_retries + 1):
            if not self.send_command(command, echonet_command):
                logging.warning(f'SKSENDTO 送信失敗 (attempt {attempt})')
                continue

            deadline = time.time() + COMMAND_TIMEOUT_SEC
            while time.time() < deadline:
                line = self._read_line()
                logging.debug(line)

                if line.startswith("ERXUDP"):
                    cols = line.strip().split(' ')
                    if len(cols) < 10:
                        continue
                    edata = cols[9]
                    esv = edata[20:22]
                    pdc = int(edata[26:28], 16)

                    if esv == "72" and pdc > 0:
                        value = int(edata[28:28 + (pdc * 2)], 16)
                        return value

            logging.warning(f'ECHONET 応答なし (attempt {attempt})')

        logging.error(f'EPC 0x{epc.hex().upper()} の取得に失敗')
        return None

    def get_measured_power(self) -> Optional[int]:
        """瞬時電力計測値（W）を取得する。"""
        value = self.send_echonet_command(b'\xE7')
        if value is None:
            return None
        return value

    def get_integrated_power(self) -> Optional[float]:
        """積算電力量計測値を取得し、HA 表示用スケールに変換して返す。"""
        # 単位を取得
        unit_code = self.send_echonet_command(b'\xE1')
        if unit_code is None:
            return None

        unit_factor = self.INTEGRATED_POWER_UNIT_TABLE.get(unit_code, 1.0)

        # 積算電力量 raw 値を取得
        raw_value = self.send_echonet_command(b'\xE0')
        if raw_value is None:
            return None

        # raw_value × unit_factor = 実際の kWh
        # さらに INTEGRATED_POWER_DISPLAY_SCALE をかけて、HA 側で `* 0.1` を不要にする
        actual_kwh = float(raw_value) * unit_factor
        display_value = actual_kwh * INTEGRATED_POWER_DISPLAY_SCALE
        return display_value


# ---------------------------------------------------------------------------
# Aki-box LED 制御
# ---------------------------------------------------------------------------

class AkiboxLed:
    def __init__(self) -> None:
        pass

    def _led(self, number: int, on: bool) -> None:
        if not 1 <= number <= 4:
            return
        path = f'/sys/class/leds/led{number}/brightness'
        try:
            with open(path, 'w') as f:
                f.write('1' if on else '0')
        except OSError:
            pass

    def clear(self) -> None:
        for n in range(4, 0, -1):
            self._led(n, False)

    def on(self, number: int) -> None:
        self._led(number, True)

    def off(self, number: int) -> None:
        self._led(number, False)


# ---------------------------------------------------------------------------
# データ検証
# ---------------------------------------------------------------------------

def is_valid_measured_power(value: Optional[int]) -> bool:
    if value is None:
        return False
    return 0 < value < MAX_MEASURED_POWER_W


def is_valid_integrated_power(value: Optional[float]) -> bool:
    if value is None:
        return False
    return 0 < value < MAX_INTEGRATED_POWER_KWH


# ---------------------------------------------------------------------------
# Home Assistant 送信
# ---------------------------------------------------------------------------

def post_to_homeassistant(
    measured_power: Optional[int],
    integrated_power: Optional[float],
) -> bool:
    """測定値を HA Webhook に POST する。"""
    if not is_valid_measured_power(measured_power):
        logging.warning(f'異常な瞬時電力値のため POST しません: {measured_power}')
        return False

    payload: dict = {'measuredpower': measured_power}
    if is_valid_integrated_power(integrated_power):
        payload['integratedpower'] = integrated_power
    else:
        logging.warning(f'積算電力量が未取得/異常のため measuredpower のみ POST: {integrated_power}')

    try:
        response = requests.post(
            HA_URL,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        response.raise_for_status()
        logging.info(f'POST 成功: {payload}')
        return True
    except Exception as e:
        logging.error(f'POST 失敗: {e}')
        return False


# ---------------------------------------------------------------------------
# メインループ
# ---------------------------------------------------------------------------

echonet = CommEchoNet()
boxled = AkiboxLed()


def main(_signum, _frame) -> None:
    boxled.on(3)
    boxled.on(4)

    measured_power = echonet.get_measured_power()
    integrated_power = echonet.get_integrated_power()

    logging.info(f"瞬時電力計測値: {measured_power}[W]")
    logging.info(f"積算電力量計測値: {integrated_power}[kWh表示値]")

    boxled.off(4)

    post_to_homeassistant(measured_power, integrated_power)
    boxled.off(3)


def _atexit() -> None:
    boxled.clear()


if __name__ == '__main__':
    atexit.register(_atexit)

    if not echonet.init_connection():
        logging.error('ECHONetデバイスと接続できませんでした.')
        sys.exit(1)

    boxled.on(2)

    signal.signal(signal.SIGALRM, main)
    signal.setitimer(signal.ITIMER_REAL, 5, UPDATE_DATA_TIME_SEC)

    while True:
        time.sleep(100)
