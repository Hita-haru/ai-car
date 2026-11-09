import logging

from control import CarController
from motor import stop_motors


# ==============================
# ログ設定
# ==============================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

logger = logging.getLogger(__name__)


# ==============================
# メイン処理
# ==============================

def main():

    controller = None

    try:
        logger.info("AI Car starting")

        # 制御クラス初期化
        controller = CarController()

        # 自律走行ループ
        while True:
            controller.run_cycle()

    except KeyboardInterrupt:
        logger.info("Program interrupted")

    except Exception:
        logger.exception("Unexpected error")

    finally:
        # 安全のため停止
        stop_motors()

        if controller is not None:
            controller.close()

        logger.info("AI Car stopped")


if __name__ == "__main__":
    main()