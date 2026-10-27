# AI Car Control Server (Gemini Robotics ER 2)

自律走行車両（AI Car）からのカメラ映像を WebSocket / HTTP でリアルタイム受信し、Google のロボティクス向け身体化推論VLM「**Gemini Robotics ER 2 preview**」によって障害物認識・空間推論・走行経路判断を行い、ステアリング舵角およびDCモーター駆動指令を即時返信するバックエンドサーバープログラムです。

- **モデル公式ドキュメント:** [Gemini Robotics ER - Overview](https://ai.google.dev/gemini-api/docs/robotics-overview?hl=ja)

---

## 📋 システム要件

- Python 3.10 以上
- Google Gemini API Key（Google AI Studio または Google Cloud Console で取得）
  - ※ 制限なし(unrestricted)キーの場合は 403 Forbidden になる場合があるため、AI Studio でキーの制限設定をご確認ください。

---

## 🚀 セットアップ手順 (仮想環境 venv の使用)

※ 実際の実稼働環境（外部サーバー / ローカルLinuxマシン等）での実行を想定しています。

### 1. 仮想環境 (venv) の作成と有効化

```bash
# programs/server ディレクトリに移動
cd programs/server

# 仮想環境の作成
python -m venv venv

# 仮想環境の有効化
# Linux / macOS:
source venv/bin/activate

# Windows:
venv\Scripts\activate
```

### 2. 必要ライブラリのインストール

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

### 3. 環境変数の設定

`.env.example` をコピーして `.env` を作成し、Gemini APIキーを設定します。

```bash
cp .env.example .env
```

`.env` 内の設定項目:
```ini
# APIキー
GEMINI_API_KEY=AIzaSy...

# モデル名 (Gemini Robotics ER 2 preview)
GEMINI_MODEL=gemini-robotics-er-2-preview

# 超低遅延・高速応答のための思考設定 (0: 思考スキップで最速応答)
GEMINI_THINKING_BUDGET=0
GEMINI_THINKING_LEVEL=low

# 車両制御パラメータ (実機ハードウェアに合わせて調整可能)
CAR_SPEED_MAX=255
CAR_SPEED_MIN=-255
CAR_SPEED_CRUISE=180
CAR_STEER_MIN=0
CAR_STEER_CENTER=90
CAR_STEER_MAX=180
CAR_DURATION_MS=500

# サーバー起動設定
HOST=0.0.0.0
PORT=8000
```

---

## 🏃 サーバーの起動

```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

- ルート確認: `http://<SERVER_IP>:8000/`
- APIドキュメント (Swagger UI): `http://<SERVER_IP>:8000/docs`
- WebSocket エンドポイント: `ws://<SERVER_IP>:8000/ws`

---

## 🔧 通信フォーマット & 制御値のカスタマイズ設計

ハードウェア（ESP32 / Raspberry Pi）側の実装進捗に合わせて、サーバー側の仕様を**簡単・安全に変更できる構造**に設計されています。

### 1. 制御値の範囲の変更
`main.py` 内の `ControlConfig` クラスまたは `.env` ファイルを編集するだけで、全処理（AIへのシステムプロンプト指示、判定値のクリップ処理、返信値）が一括連動して変更されます。

```python
class ControlConfig:
    SPEED_MAX: int = 255            # 前進最大値
    SPEED_MIN: int = -255           # 後進最大値
    SPEED_STOP: int = 0             # 停止値
    SPEED_CRUISE_DEFAULT: int = 180 # 巡航速度
    STEER_MIN: int = 0              # 左最大舵角
    STEER_CENTER: int = 90          # 直進舵角
    STEER_MAX: int = 180            # 右最大舵角
    DURATION_MS: int = 500          # 1サイクルの駆動時間 (500ms)
```

### 2. 通信フォーマット（JSONキー・階層構造）の変更
`main.py` の以下の2つの関数を編集するだけで、送受信のJSON構造を自由に変更できます。

- **受信パケットの変更:** `parse_incoming_packet(packet: dict) -> Optional[CarInputPacket]`
  - ESP32から送られるJSONのキー名やネスト構造を変更したい場合は、この関数の抽出処理を調整します。
- **送信パケットの変更:** `build_outgoing_packet(...) -> dict`
  - ESP32 / ラズパイが受信したいJSONキー（フラット型、ヘッダー/ボディ構造など）をこの関数内で定義します。
  - ※ 現在はフラット型（`speed`, `angle`, `motor`, `steering`）と階層型（`header`, `body`）の両立フォーマットをデフォルトとして返信します。

---

## ⚡ 高速化（低遅延）設計のポイント

1. **完全非同期（Async I/O）**
   - Google公式 `google-genai` SDKの `client.aio.models.generate_content` を使用し、推論待ち中もイベントループを一切ブロックしません。
2. **推論思考時間（Thinking）の最小化**
   - 走行判断の即時応答のため、`thinking_budget=0` / `thinking_level="low"` を設定し、レスポンス遅延を極小化。
3. **高速JSONパーサー（`orjson`）**
   - C言語実装の高速JSONエンコーダー/デコーダーを採用。
4. **バイナリ直渡し画像パイプライン**
   - ラズパイ側ですでにJPEGエンコードされているため、サーバー側での無駄な再エンコードを行わず、バイト列をそのままGemini APIに渡す高効率設計。
5. **フェイルセーフ**
   - API通信エラーやパケット破損時は、即座に車両停止パケット（`speed: 0, angle: 90`）を返信。
