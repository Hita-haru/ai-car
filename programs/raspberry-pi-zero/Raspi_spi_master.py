
import spidev
import json
import time


# ESP32側の仕様確認後に設定する
ACK_BUFFER_SIZE = None

# 旧コードにあったNULL終端を暫定的に維持
PACKET_TERMINATOR = "\0"


class SPIMaster:
    def __init__(
        self,
        bus=0,
        device=0,
        max_speed_hz=1000000,
        cs_control=None
    ):
        self.spi = spidev.SpiDev()
        self.spi.open(bus, device)
        self.spi.max_speed_hz = max_speed_hz
        self.spi.mode = 0

        # 外部GPIOでCSを制御する
        self.cs_control = cs_control

        if self.cs_control is not None:
            self.spi.no_cs = True

    # JSONパケット作成
    def build_packet(self, packet_type, data, destination="ESP32"):
        body = {
            "data": data
        }

        # body全体をコンパクトなJSONにしてチェックサムを計算
        body_text = json.dumps(
            body,
            ensure_ascii=False,
            separators=(",", ":")
        )

        checksum = 0
        for byte in body_text.encode("utf-8"):
            checksum ^= byte

        packet = {
            "header": {
                "type": packet_type,
                "destination": destination
            },
            "body": body,
            "footer": {
                "checksum": checksum,
                "time": time.time()
            }
        }

        return packet

    # JSONパケットをSPI送信
    def send_packet(self, packet):
        json_text = json.dumps(
            packet,
            ensure_ascii=False,
            separators=(",", ":")
        ) + PACKET_TERMINATOR

        tx_bytes = list(json_text.encode("utf-8"))

        self.spi.xfer2(tx_bytes)

    # ESP32からACKを受信
    def receive_ack(self):
        if ACK_BUFFER_SIZE is None:
            raise NotImplementedError(
                "ACK_BUFFER_SIZEをESP32側の仕様に合わせて設定してください"
            )

        rx_bytes = self.spi.xfer2([0] * ACK_BUFFER_SIZE)

        rx_text = bytes(
            byte for byte in rx_bytes if byte != 0
        ).decode("utf-8", errors="ignore").strip()

        if not rx_text:
            return None

        try:
            packet = json.loads(rx_text)
        except json.JSONDecodeError:
            return None

        if packet.get("header", {}).get("type") != "ACK":
            return None

        return packet

    # 指定時間内にACKを待つ
    def wait_ack(self, timeout):
        start_time = time.monotonic()

        while time.monotonic() - start_time < timeout:
            ack = self.receive_ack()

            if ack is not None:
                return ack

            time.sleep(0.01)

        return None

    # CSをLOWにする
    def start_session(self):
        if self.cs_control is None:
            raise RuntimeError(
                "CSをLOWにするGPIO制御が設定されていません"
            )

        self.cs_control(False)

    # CSをHIGHにする
    def end_session(self):
        if self.cs_control is not None:
            self.cs_control(True)

    # INI送信：500msタイムアウト、最大3回
    def send_ini(self):
        packet = self.build_packet(
            "INI",
            "INIT_START"
        )

        for attempt in range(3):
            print(f"Sending INI ({attempt + 1}/3)")

            self.send_packet(packet)
            ack = self.wait_ack(0.5)

            if ack is not None:
                print("INI ACK received")
                return True

        print("INI communication failed")
        return False

    # STR送信
    def send_str(self, chunks):
        raise NotImplementedError(
            "STRの分割番号・ACKの欠損通知形式が未確定です。"
            "MSprotocol.mdとESP32側の実装確認後に実装してください。"
        )

    # FIN送信：200msタイムアウト、最大3回
    def send_fin(self):
        packet = self.build_packet(
            "FIN",
            "TRANSFER_END"
        )

        for attempt in range(3):
            print(f"Sending FIN ({attempt + 1}/3)")

            self.send_packet(packet)
            ack = self.wait_ack(0.2)

            if ack is not None:
                print("Final ACK received")
                return True

        print("FIN communication failed")
        return False

    # SPI終了
    def close(self):
        self.end_session()
        self.spi.close()


if __name__ == "__main__":
    # TODO: 実際のGPIOを使用したCS制御関数を設定する
    master = SPIMaster()

    try:
        master.start_session()

        if master.send_ini():
            print("INI communication completed")

            # TODO: STR送信処理を実装
            # master.send_str(chunks)

            # STR処理完了後にFINを送信
            # master.send_fin()

    except Exception as error:
        print("SPI Error:", error)

    finally:
        master.close()
