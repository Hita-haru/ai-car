import time
import logging
import cv2

from raspi_spi_master import SPIMaster, STR_CHUNK_SIZE
from camera import capture_frame_as_base64
from motor import move_motor, steer_motor, stop_motors


logger = logging.getLogger(__name__)


class CarController:

    def __init__(self):
        # USBカメラを開く
        self.camera = cv2.VideoCapture(0)

        if not self.camera.isOpened():
            raise RuntimeError("Camera could not be opened")

        # SPI初期化
        self.spi = SPIMaster()

    # 画像データを分割
    def split_image_data(self, image_base64):
        chunks = []

        for i in range(0, len(image_base64), STR_CHUNK_SIZE):
            chunks.append(
                image_base64[i:i + STR_CHUNK_SIZE]
            )

        return chunks

    # カメラ画像を取得
    def get_camera_image(self):
        image_base64 = capture_frame_as_base64(self.camera)

        if not image_base64:
            raise ValueError("Camera image capture failed")

        return image_base64

    # 画像をESP32へ送信 (修正箇所: チャンク送信後のACK待機ロジック変更)
    def send_camera_image(self, image_base64):
        chunks = self.split_image_data(image_base64)
        total_chunks = len(chunks)

        logger.info("Image size: %d characters", len(image_base64))
        logger.info("Total chunks: %d", total_chunks)

        # INI送信
        if not self.spi.send_ini(total_chunks):
            logger.error("INI communication failed")
            return self.spi.parse_control(None)

        # STR送信 (ここでは送信のみを連続で行う)
        for index, chunk in enumerate(chunks, start=1):
            if not self.spi.send_chunk(chunk, index):
                logger.error("Image transfer failed")
                return self.spi.parse_control(None)

        # チャンク全送信後に一括でACKを確認
        if not self.spi.wait_str_ack():
            logger.error("STR chunks rejected or timeout")
            return self.spi.parse_control(None)

        # FIN送信
        fin_packet_number = total_chunks + 1
        final_ack = self.spi.send_fin(fin_packet_number)

        # 制御値取得
        return self.spi.parse_control(final_ack)

    # モーター制御
    def apply_control(self, control):
        speed = control["speed"]
        angle = control["angle"]
        duration = control["duration_ms"]

        logger.info(
            "Motor control: speed=%d angle=%d duration=%dms",
            speed,
            angle,
            duration
        )

        steer_motor(angle)
        move_motor(speed)

        time.sleep(duration / 1000.0)

        stop_motors()

    # 1回分の自律走行処理
    def run_cycle(self):
        try:
            image_base64 = self.get_camera_image()

            control = self.send_camera_image(image_base64)

            self.apply_control(control)

        except Exception:
            logger.exception("Control cycle failed")
            stop_motors()

    # 終了処理
    def close(self):
        stop_motors()
        self.camera.release()
        self.spi.close()