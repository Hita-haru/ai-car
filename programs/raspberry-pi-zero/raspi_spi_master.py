import spidev
import json
import time
import uuid

from datetime import datetime
from gpiozero import DigitalInputDevice


# ==============================
# SPI設定
# ==============================

SPI_BUS = 0
SPI_DEVICE = 0
SPI_SPEED_HZ = 1000000
SPI_MODE = 0

# Raspberry Pi GPIO (BCM)
SPI_READY_PIN = 25

# 通信サイズ
ACK_BUFFER_SIZE = 256
SPI_FRAME_SIZE = 4096
STR_CHUNK_SIZE = 2048

# タイムアウト・再送回数
READY_TIMEOUT = 0.5
INI_MAX_RETRIES = 3
STR_MAX_RETRIES = 3
FIN_MAX_RETRIES = 3

FIN_TIMEOUT = 0.2
# 修正箇所: AIサーバーの推論時間(0.4秒)とネットワーク遅延を考慮し、余裕を持たせて1.0秒に延長
CONTROL_TIMEOUT = 1.0 

# 通信先・データ種別
DESTINATION = "SLV"
DATA_TYPE = "IMG"
INI_DATA = "INIT_START"


# ==============================
# SPI Master クラス
# ==============================

class SPIMaster:

    def __init__(
        self,
        bus=SPI_BUS,
        device=SPI_DEVICE,
        max_speed_hz=SPI_SPEED_HZ
    ):
        self.spi = spidev.SpiDev()
        self.spi.open(bus, device)

        self.spi.max_speed_hz = max_speed_hz
        self.spi.mode = SPI_MODE
        self.spi.bits_per_word = 8

        # ESP32からのSPI_READY入力
        self.ready = DigitalInputDevice(
            SPI_READY_PIN,
            pull_up=False
        )

        # 通信セッションID
        self.session_id = uuid.uuid4().hex[:12]

        print("SPI Master initialized")
        print("Session ID:", self.session_id)

    # SPI終了
    def close(self):
        self.spi.close()
        self.ready.close()

        print("SPI Master closed")

    # XORチェックサム計算
    def calculate_xor_checksum(self, data):
        checksum = 0

        for byte in data.encode("utf-8"):
            checksum ^= byte

        return checksum

    # 現在時刻取得
    def get_timestamp(self):
        return datetime.now().strftime("%Y%m%d%H%M%S")

    # SPI_READY待機
    def wait_spi_ready(self, timeout=READY_TIMEOUT):
        if self.ready.value:
            return True

        result = self.ready.wait_for_active(timeout=timeout)

        if result is None:
            print("SPI_READY timeout")
            return False

        return True

    # パケット作成
    def build_packet(
        self,
        packet_type,
        data=None,
        packet_number=0,
        total_chunks=None
    ):
        header = {
            "type": packet_type,
            "dest": DESTINATION,
            "snum": self.session_id
        }

        body = {}

        if packet_type == "INI":
            body = {
                "fin": total_chunks,
                "data": INI_DATA,
                "type": DATA_TYPE
            }

        elif packet_type == "STR":
            body = {
                "data": data,
                "type": DATA_TYPE
            }

        elif packet_type == "FIN":
            body = {}

        # チェックサム対象
        if data is not None:
            checksum_data = data
        elif packet_type == "INI":
            checksum_data = INI_DATA
        else:
            checksum_data = ""

        footer = {
            "chks": self.calculate_xor_checksum(checksum_data),
            "time": self.get_timestamp(),
            "pnum": packet_number
        }

        packet = {
            "header": header,
            "body": body,
            "footer": footer
        }

        return packet

    # JSONパケットをSPI送信用データに変換
    def encode_packet(self, packet):
        json_data = json.dumps(
            packet,
            ensure_ascii=False,
            separators=(",", ":")
        )

        tx_data = json_data.encode("utf-8")

        if len(tx_data) > SPI_FRAME_SIZE:
            raise ValueError(
                f"Packet too large: {len(tx_data)} bytes"
            )

        # ESP32側の4096バイト受信バッファに合わせる
        tx_data = tx_data.ljust(SPI_FRAME_SIZE, b"\0")

        return list(tx_data)

    # ACK JSON解析
    def decode_ack(self, rx_data):
        raw = bytes(rx_data)

        # NULL終端・ゼロパディングを除去
        raw = raw.split(b"\0", 1)[0].strip()

        if not raw:
            print("Empty ACK received")
            return None

        try:
            ack = json.loads(raw.decode("utf-8"))

            if not isinstance(ack, dict):
                return None

            return ack

        except (UnicodeDecodeError, json.JSONDecodeError):
            print("Invalid ACK JSON")
            return None

    # ACK受信
    def receive_ack(self, timeout=READY_TIMEOUT):
        if not self.wait_spi_ready(timeout):
            return None

        # Masterがクロックを供給してACKを読み出す
        rx_data = self.spi.xfer2(
            [0x00] * ACK_BUFFER_SIZE
        )

        ack = self.decode_ack(rx_data)

        if ack is None:
            print("ACK decode failed")
            return None

        print(
            "ACK received:",
            self.get_ack_status(ack)
        )

        return ack

    # ACK待機
    def wait_ack(self, timeout=READY_TIMEOUT):
        return self.receive_ack(timeout)

    # SPIパケット送信
    def send_packet(self, packet):
        if not self.wait_spi_ready():
            return False

        tx_data = self.encode_packet(packet)

        # xfer2中はCE0がLOWになり、転送終了後にHIGHへ戻る
        self.spi.xfer2(tx_data)

        return True

    # ACK状態取得
    def get_ack_status(self, ack):
        if ack is None:
            return None

        header = ack.get("header", {})

        return header.get("status")

    # INI送信
    def send_ini(self, total_chunks):
        packet = self.build_packet(
            "INI",
            packet_number=0,
            total_chunks=total_chunks
        )

        for attempt in range(1, INI_MAX_RETRIES + 1):
            print(f"Sending INI ({attempt}/{INI_MAX_RETRIES})")

            if not self.send_packet(packet):
                continue

            ack = self.wait_ack(READY_TIMEOUT)

            if self.get_ack_status(ack) == "OK":
                print("INI ACK received")
                return True

        print("INI communication failed")
        return False

    # STRチャンク送信（修正箇所：ACKを待たずに送信のみ行う）
    def send_chunk(self, chunk_data, chunk_number):
        packet = self.build_packet(
            "STR",
            data=chunk_data,
            packet_number=chunk_number
        )
        print(f"Sending STR chunk {chunk_number}")
        
        return self.send_packet(packet)

    # チャンク全送信後のACK一括待機（新設関数）
    def wait_str_ack(self):
        print("Waiting for STR ACK...")
        ack = self.wait_ack(READY_TIMEOUT)
        status = self.get_ack_status(ack)
        
        if status == "OK":
            print("All STR chunks accepted")
            return True
        elif status == "RESEND_REQ":
            # 再送要求の処理が別途必要な場合はここに記述
            print("Resend requested from Slave")
            return False
        
        return False

    # FIN送信（修正箇所：ループを適切に抜けるように修正）
    def send_fin(self, packet_number):
        packet = self.build_packet(
            "FIN",
            packet_number=packet_number
        )

        for attempt in range(1, FIN_MAX_RETRIES + 1):
            print(f"Sending FIN ({attempt}/{FIN_MAX_RETRIES})")

            if not self.send_packet(packet):
                continue

            # FIN受信後、制御データの準備を待つ
            ack = self.wait_ack(CONTROL_TIMEOUT)
            status = self.get_ack_status(ack)

            if status == "CONTROL_READY":
                print("Control data received")
                return ack

            if status == "OK":
                # FIN受理のACKが先に来た場合は、制御値をもう一度待つ
                print("FIN accepted, waiting for control data...")
                ack_control = self.wait_ack(CONTROL_TIMEOUT)
                if self.get_ack_status(ack_control) == "CONTROL_READY":
                    print("Control data received")
                    return ack_control

        print("FIN failed or control response timeout")
        return None

    # 制御値取得
    def parse_control(self, ack):
        safe_control = {
            "speed": 0,
            "angle": 90,
            "duration_ms": 200,
            "reason": "failsafe"
        }

        if ack is None:
            print("No control ACK. Emergency stop.")
            return safe_control

        if self.get_ack_status(ack) != "CONTROL_READY":
            print("Control data not ready")
            return safe_control

        body = ack.get("body", {})

        try:
            speed = int(body["speed"])
            angle = int(body["angle"])
            duration = int(body.get("duration_ms", 500))

            if not -255 <= speed <= 255:
                raise ValueError("Invalid speed")

            if not 0 <= angle <= 180:
                raise ValueError("Invalid angle")

            if duration <= 0:
                raise ValueError("Invalid duration")

            return {
                "speed": speed,
                "angle": angle,
                "duration_ms": duration,
                "reason": body.get("reason", "")
            }

        except (KeyError, TypeError, ValueError):
            print("Invalid control data. Emergency stop.")
            return safe_control