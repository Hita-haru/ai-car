"""
AI Car Autonomous Driving Server
================================
- VLM (視覚言語モデル) として Google の「Gemini Robotics ER 2 preview」を使用
- ESP32 / Raspberry Pi Zero と WebSocket (リアルタイム双方向) および HTTP で通信
- 500ms サイクル走行に対応する超高速・低遅延 (Async I/O + orjson) 設計
- 将来の「通信フォーマット」や「制御値の範囲」の変更を容易に行えるモジュール構造
"""

import os
import sys
import base64
import logging
from typing import Optional, Any, Dict
from dataclasses import dataclass

# 高速JSON処理ライブラリ (環境にあれば orjson を使用、なければ標準 json にフォールバック)
try:
    import orjson
    HAS_ORJSON = True
except ImportError:
    HAS_ORJSON = False
    orjson = None  # type: ignore
    import json

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from dotenv import load_dotenv

# Google GenAI 公式最新SDK
from google import genai
from google.genai import types

# ログ設定
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("ai-car-server")

# 環境変数の読み込み (.env)
load_dotenv()


# =====================================================================
# 1. 車両制御パラメータ設定 (ここを変更することで制御範囲を容易に調整可能)
# =====================================================================
class ControlConfig:
    """
    車両のモーター速度・ステアリング舵角の許容範囲とデフォルト設定。
    実機のモーターやサーボの特性に合わせてここまたは .env で変更してください。
    """
    # DCモーター速度設定 (ik-motor.py 準拠: -255〜255)
    SPEED_MAX: int = int(os.getenv("CAR_SPEED_MAX", "255"))          # 前進の最大出力
    SPEED_MIN: int = int(os.getenv("CAR_SPEED_MIN", "-255"))         # 後進の最大出力 (負値)
    SPEED_STOP: int = 0                                              # 停止出力
    SPEED_CRUISE_DEFAULT: int = int(os.getenv("CAR_SPEED_CRUISE", "180")) # 通常巡航速度 (推奨: 120〜200)

    # ステアリングサーボ角度設定 (ik-motor.py 準拠: 0〜180度)
    STEER_MIN: int = int(os.getenv("CAR_STEER_MIN", "0"))            # 左最大舵角
    STEER_CENTER: int = int(os.getenv("CAR_STEER_CENTER", "90"))     # 直進・中立舵角
    STEER_MAX: int = int(os.getenv("CAR_STEER_MAX", "180"))          # 右最大舵角

    # 1サイクルあたりの駆動持続時間 (手書きノート仕様: モーターは500ms)
    DURATION_MS: int = int(os.getenv("CAR_DURATION_MS", "500"))


# =====================================================================
# 2. AIモデル設定 (Gemini Robotics ER 2 preview)
# =====================================================================
class AIConfig:
    """
    Gemini Robotics モデルに関する設定
    詳細は https://ai.google.dev/gemini-api/docs/robotics-overview?hl=ja を参照
    """
    API_KEY: str = os.getenv("GEMINI_API_KEY", "")
    # Gemini Robotics ER 2 preview の標準モデルエンドポイントコード
    MODEL_NAME: str = os.getenv("GEMINI_MODEL", "gemini-robotics-er-2-preview")
    
    # 低遅延・高速応答のための設定 (thinking レベルまたはバジェット)
    # リアルタイム走行制御のため、推論遅延を最小化する設定を推奨
    THINKING_LEVEL: str = os.getenv("GEMINI_THINKING_LEVEL", "low")  # "low", "medium", "high"
    THINKING_BUDGET: int = int(os.getenv("GEMINI_THINKING_BUDGET", "0"))


# Geminiクライアントの初期化
gemini_client: Optional[genai.Client] = None
if AIConfig.API_KEY:
    try:
        gemini_client = genai.Client(api_key=AIConfig.API_KEY)
        logger.info(f"Gemini Robotics Client initialized successfully with model: {AIConfig.MODEL_NAME}")
    except Exception as e:
        logger.error(f"Failed to initialize Gemini Client: {e}")
else:
    logger.warning("GEMINI_API_KEY is not set. Server will operate in mock/failsafe mode.")


# =====================================================================
# 3. 高速JSONヘルパー
# =====================================================================
def fast_json_loads(data: str | bytes) -> dict:
    """高速JSONパース"""
    if orjson is not None:
        return orjson.loads(data)
    return json.loads(data)


def fast_json_dumps(data: Any) -> str:
    """高速JSONシリアライズ"""
    if orjson is not None:
        return orjson.dumps(data).decode("utf-8")
    return json.dumps(data, ensure_ascii=False)


def extract_base64_bytes(base64_str: str) -> bytes:
    """Base64文字列からバイナリ画像データを高速抽出・デコード"""
    if not base64_str:
        raise ValueError("Empty image string")
    
    # data:image/jpeg;base64, などのプレフィックスがある場合は除去
    if "," in base64_str:
        base64_str = base64_str.split(",", 1)[1]
    
    base64_str = base64_str.strip()
    missing_padding = len(base64_str) % 4
    if missing_padding:
        base64_str += "=" * (4 - missing_padding)
        
    return base64.b64decode(base64_str)


# =====================================================================
# 4. 通信フォーマット定義・カスタマイズ関数 (容易に変更可能な設計)
# =====================================================================
@dataclass
class CarInputPacket:
    """受信した生パケットから抽出した共通入力データ構造体"""
    image_bytes: bytes
    telemetry: Optional[dict] = None
    sequence: Optional[int] = None
    raw_packet: Optional[dict] = None


def parse_incoming_packet(packet: dict) -> Optional[CarInputPacket]:
    """
    【受信フォーマットのカスタマイズ関数】
    ESP32 / Raspberry Pi から受信した辞書オブジェクトから、画像データや状態情報を抽出します。
    将来フォーマットが変更された場合、この関数内のキー名や探索ロジックを調整してください。
    """
    # 1. 画像データの抽出 (キー名候補: image, image_base64, img, data または body 直下)
    base64_str = (
        packet.get("image") or 
        packet.get("image_base64") or 
        packet.get("img") or 
        packet.get("data")
    )
    if not base64_str and isinstance(packet.get("body"), dict):
        body = packet["body"]
        base64_str = body.get("image") or body.get("img") or body.get("data")

    if not base64_str:
        return None

    image_bytes = extract_base64_bytes(base64_str)

    # 2. 車両ステータス・テレメトリ情報の抽出 (任意)
    telemetry = packet.get("status") or packet.get("telemetry")
    if not telemetry and isinstance(packet.get("body"), dict):
        telemetry = packet["body"].get("status")

    # 3. 通信シーケンス番号の抽出 (パケット損失検出用)
    sequence = packet.get("sequence") or packet.get("seq")
    if sequence is None and isinstance(packet.get("header"), dict):
        sequence = packet["header"].get("sequence") or packet["header"].get("seq")

    return CarInputPacket(
        image_bytes=image_bytes,
        telemetry=telemetry,
        sequence=sequence,
        raw_packet=packet
    )


def build_outgoing_packet(
    speed: int,
    angle: int,
    reason: str,
    status: str = "success",
    error_msg: Optional[str] = None,
    sequence: Optional[int] = None
) -> dict:
    """
    【送信フォーマットのカスタマイズ関数】
    ESP32 / Raspberry Pi へ返信するJSON辞書を組み立てます。
    ESP32側のパーサーの仕様が決まり次第、この関数の辞書構造を調整してください。

    現在: フラット型 (speed, angle, duration_ms 等) と
          階層型 (header, body: esp32.ino互換) の双方に対応した互換構造を返します。
    """
    return {
        # --- 基本制御値 ---
        "status": status,
        "speed": speed,
        "angle": angle,
        "duration_ms": ControlConfig.DURATION_MS,
        "reason": reason,
        "error": error_msg,
        
        # --- ラズパイ ik-motor.py 互換用エイリアス ---
        "motor": speed,
        "steering": angle,

        # --- ESP32 ヘッダー・ボディ構造互換 (esp32.ino 内 doc["header"]["type"] 参照用) ---
        "header": {
            "type": 1 if not error_msg else -1,
            "status": status,
            "sequence": sequence
        },
        "body": {
            "speed": speed,
            "angle": angle,
            "duration_ms": ControlConfig.DURATION_MS,
            "reason": reason
        }
    }


# =====================================================================
# 5. データモデル定義 (Pydantic / Structured Outputs)
# =====================================================================
class AIControlDecision(BaseModel):
    """Gemini Robotics APIから構造化出力 (Structured Output) として受け取るモデル"""
    speed: int = Field(
        ...,
        description=f"DC motor speed between {ControlConfig.SPEED_MIN} and {ControlConfig.SPEED_MAX}. 0 is stop, positive is forward, negative is reverse."
    )
    angle: int = Field(
        ...,
        description=f"Steering servo angle between {ControlConfig.STEER_MIN} and {ControlConfig.STEER_MAX} degrees. {ControlConfig.STEER_MIN} is full left, {ControlConfig.STEER_CENTER} is center straight, {ControlConfig.STEER_MAX} is full right."
    )
    reason: str = Field(
        ...,
        description="Embodied spatial reasoning justifying the chosen movement (e.g. obstacles detected, clear path ahead)."
    )


class ControlResponse(BaseModel):
    """HTTP/WebSocket レスポンス型"""
    status: str = "success"
    speed: int
    angle: int
    motor: int
    steering: int
    duration_ms: int = ControlConfig.DURATION_MS
    reason: str
    error: Optional[str] = None
    header: Optional[Dict[str, Any]] = None
    body: Optional[Dict[str, Any]] = None


class Imgdata(BaseModel):
    """HTTP POST /sendimg/ 受信用リクエストモデル"""
    image: str = Field(..., description="Base64-encoded image string")
    status: Optional[dict] = Field(default=None, description="Optional telemetry data")
    sequence: Optional[int] = Field(default=None, description="Optional packet sequence number")



# =====================================================================
# 6. AIプロンプトの動的構築 (ControlConfigの設定に自動連動)
# =====================================================================
def get_system_instruction() -> str:
    """ControlConfigの設定値を反映したシステムプロンプトを生成"""
    return f"""\
You are an expert autonomous robotics navigation AI (Gemini Robotics ER) controlling a physical robot car.
You are provided with real-time camera views from the front of the vehicle.
Apply embodied reasoning and spatial understanding to navigate safely and avoid collisions.

Control Range Specifications:
1. DC Motor Speed (`speed`):
   - Range: {ControlConfig.SPEED_MIN} to {ControlConfig.SPEED_MAX}
   - Cruising forward: around {ControlConfig.SPEED_CRUISE_DEFAULT} (safe moderate speed)
   - Stop: {ControlConfig.SPEED_STOP} (when path is blocked, cliff detected, or danger ahead)
   - Reverse: around -150 (only when trapped or immediately blocked by an obstacle)
2. Steering Angle (`angle`):
   - Range: {ControlConfig.STEER_MIN} to {ControlConfig.STEER_MAX} degrees
   - Straight center: {ControlConfig.STEER_CENTER}
   - Steer Left: {ControlConfig.STEER_MIN} to {ControlConfig.STEER_CENTER - 1} (smaller number = sharper left)
   - Steer Right: {ControlConfig.STEER_CENTER + 1} to {ControlConfig.STEER_MAX} (larger number = sharper right)
3. Reason (`reason`):
   - Describe visible obstacles, open free space, and the spatial reason for your action in a concise sentence.

Output MUST adhere strictly to the JSON schema.
"""


def create_failsafe_response(reason: str, error_msg: Optional[str] = None, sequence: Optional[int] = None) -> dict:
    """異常時や通信切断時に車両を安全停止させるフェイルセーフパケット"""
    return build_outgoing_packet(
        speed=ControlConfig.SPEED_STOP,
        angle=ControlConfig.STEER_CENTER,
        reason=reason,
        status="failsafe" if error_msg else "mock",
        error_msg=error_msg,
        sequence=sequence
    )


# =====================================================================
# 7. AI推論コア処理 (完全非同期 & 低遅延)
# =====================================================================
async def process_image_and_decide(image_bytes: bytes, telemetry: Optional[dict] = None, sequence: Optional[int] = None) -> dict:
    """
    Gemini Robotics ER 2 preview に画像を送信し、リアルタイム走行指示を取得する。
    非同期 (Async) で実行され、イベントループをブロックしません。
    """
    if not gemini_client:
        logger.warning("No Gemini client available; returning failsafe stop response.")
        return create_failsafe_response(
            reason="Gemini API Key is not configured on server. Vehicle remains safely stopped.",
            error_msg="GEMINI_API_KEY_NOT_CONFIGURED",
            sequence=sequence
        )
    
    user_prompt = "Analyze this front camera view using spatial reasoning and decide the next driving action (speed, angle, reason)."
    if telemetry:
        user_prompt += f" Current telemetry: {telemetry}"

    try:
        # 思考設定 (Thinking Config)
        # 低遅延を重視し、thinking_budget または thinking_level を指定
        thinking_conf = None
        if hasattr(types, "ThinkingConfig"):
            try:
                if AIConfig.THINKING_BUDGET >= 0:
                    thinking_conf = types.ThinkingConfig(thinking_budget=AIConfig.THINKING_BUDGET)
                elif AIConfig.THINKING_LEVEL:
                    thinking_conf = types.ThinkingConfig(thinking_level=AIConfig.THINKING_LEVEL)
            except Exception as e:
                logger.debug(f"ThinkingConfig setup note: {e}")

        config_params = {
            "response_mime_type": "application/json",
            "response_schema": AIControlDecision,
            "system_instruction": get_system_instruction(),
            "temperature": 0.2,
        }
        if thinking_conf is not None:
            config_params["thinking_config"] = thinking_conf

        config = types.GenerateContentConfig(**config_params)

        # Gemini Robotics ER 2 preview を非同期クライアント (aio) で呼び出し
        response = await gemini_client.aio.models.generate_content(
            model=AIConfig.MODEL_NAME,
            contents=[
                types.Part.from_bytes(
                    data=image_bytes,
                    mime_type="image/jpeg",
                ),
                user_prompt
            ],
            config=config,
        )
        
        # 構造化出力のパース
        parsed_result = None
        if hasattr(response, "parsed") and response.parsed:
            parsed_result = response.parsed
        elif response.text:
            parsed_json = fast_json_loads(response.text)
            parsed_result = AIControlDecision(**parsed_json)
        
        if not parsed_result:
            raise ValueError("Failed to obtain structured output from Gemini Robotics model")

        # 設定範囲 (ControlConfig) 内にクリップ
        speed = max(ControlConfig.SPEED_MIN, min(ControlConfig.SPEED_MAX, int(parsed_result.speed)))
        angle = max(ControlConfig.STEER_MIN, min(ControlConfig.STEER_MAX, int(parsed_result.angle)))
        
        logger.info(f"AI Decision [{AIConfig.MODEL_NAME}] -> speed: {speed}, angle: {angle}, reason: {parsed_result.reason}")

        return build_outgoing_packet(
            speed=speed,
            angle=angle,
            reason=parsed_result.reason,
            status="success",
            sequence=sequence
        )

    except Exception as e:
        logger.error(f"Error during Gemini Robotics API inference: {e}", exc_info=True)
        return create_failsafe_response(
            reason="Inference failed; emergency stop executed for safety.",
            error_msg=str(e),
            sequence=sequence
        )


# =====================================================================
# 8. FastAPI アプリケーションおよびルーティング
# =====================================================================
app = FastAPI(
    title="AI Car Autonomous Driving Server",
    description="Backend server for autonomous AI car with ESP32 & Raspberry Pi Zero, powered by Gemini Robotics ER 2 preview.",
    version="2.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def homepage():
    """サーバー稼働ステータス"""
    return {
        "status": "online",
        "service": "AI Car Robotics Server",
        "model": AIConfig.MODEL_NAME,
        "api_configured": bool(gemini_client),
        "control_ranges": {
            "speed": [ControlConfig.SPEED_MIN, ControlConfig.SPEED_MAX],
            "steer": [ControlConfig.STEER_MIN, ControlConfig.STEER_MAX],
            "duration_ms": ControlConfig.DURATION_MS
        }
    }


@app.get("/health")
def healthcheck():
    """ヘルスチェックエンドポイント"""
    return {
        "status": "ok",
        "gemini_api_key_set": bool(AIConfig.API_KEY),
        "model": AIConfig.MODEL_NAME
    }


@app.post("/sendimg/", response_model=ControlResponse)
@app.post("/api/control", response_model=ControlResponse)
async def http_control_endpoint(gimg: Imgdata):
    """HTTP POST による画像送信・制御指令取得エンドポイント (テスト/フォールバック用)"""
    try:
        image_bytes = extract_base64_bytes(gimg.image)
    except Exception as e:
        logger.warning(f"Invalid image data via HTTP: {e}")
        raise HTTPException(status_code=400, detail=f"Invalid base64 image data: {e}")

    result_dict = await process_image_and_decide(image_bytes, telemetry=gimg.status, sequence=gimg.sequence)
    return ControlResponse(**result_dict)


# =====================================================================
# 9. WebSocket エンドポイント (実機走行用 リアルタイム通信)
# =====================================================================
@app.websocket("/ws")
@app.websocket("/ws/control")
async def websocket_control_endpoint(websocket: WebSocket):
    """
    WebSocket通信エンドポイント
    ESP32から送られるパケットを受信し、Gemini Robotics推論結果を即時返信します。
    """
    await websocket.accept()
    client_host = websocket.client.host if websocket.client else "unknown"
    logger.info(f"[WebSocket] Connected client: {client_host}")

    try:
        while True:
            # 1. パケットの受信
            data = await websocket.receive_text()
            
            try:
                raw_packet = fast_json_loads(data)
            except Exception:
                logger.warning(f"[WebSocket] Non-JSON message received: {data[:100]}")
                failsafe = create_failsafe_response("Invalid JSON packet format", "JSON_PARSE_ERROR")
                await websocket.send_text(fast_json_dumps(failsafe))
                continue

            # 2. 受信パケットのパース (カスタマイズ関数利用)
            input_data = parse_incoming_packet(raw_packet)
            if not input_data:
                logger.warning("[WebSocket] Received JSON packet missing valid image field")
                failsafe = create_failsafe_response("Missing or invalid image field in payload", "MISSING_IMAGE")
                await websocket.send_text(fast_json_dumps(failsafe))
                continue

            # 3. AIによる判断処理 (完全非同期・低遅延)
            control_dict = await process_image_and_decide(
                image_bytes=input_data.image_bytes,
                telemetry=input_data.telemetry,
                sequence=input_data.sequence
            )

            # 4. ESP32へ高速返信
            await websocket.send_text(fast_json_dumps(control_dict))
            logger.info(
                f"[WebSocket] Replied: speed={control_dict.get('speed')}, "
                f"angle={control_dict.get('angle')} ({control_dict.get('duration_ms')}ms)"
            )

    except WebSocketDisconnect:
        logger.info(f"[WebSocket] Disconnected client: {client_host}")
    except Exception as e:
        logger.error(f"[WebSocket] Unexpected WebSocket error: {e}", exc_info=True)
