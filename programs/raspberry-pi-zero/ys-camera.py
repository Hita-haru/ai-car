import base64
import cv2


def capture_frame_as_base64(cap: cv2.VideoCapture) -> str:
    """USBカメラからフレームを取得し、160x120・JPEG品質50でBase64エンコードして返す関数"""
    if not cap.isOpened():
        print("エラー: カメラが開かれていません。")
        return ""

    ret, frame = cap.read()
    if not ret or frame is None:
        print("エラー: フレームの取得に失敗しました。")
        return ""

    # 1. 解像度を 160x120 にリサイズ
    resized_frame = cv2.resize(frame, (160, 120))

    # 2. JPEG圧縮の設定 (品質: 50)
    encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), 50]

    # 3. メモリ上でJPEGエンコード
    result, buffer = cv2.imencode(".jpg", resized_frame, encode_param)
    if not result:
        print("エラー: JPEGエンコードに失敗しました。")
        return ""

    # 4. Base64文字列へ変換
    base64_str = base64.b64encode(buffer).decode("utf-8")
    return base64_str


def main():
    # USBカメラのオープン (0: 標準のUSBカメラID)
    cap = cv2.VideoCapture(0)

    try:
        base64_image = capture_frame_as_base64(cap)
        if base64_image:
            print(f"Base64画像取得成功 (文字数: {len(base64_image)})")
            print(f"先頭50文字: {base64_image[:50]}...")
    finally:
        cap.release()


if __name__ == "__main__":
    main()