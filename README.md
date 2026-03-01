# 🦀 boiled-claw

> Your personal AI agent powered by Google ADK and Gemini 3.0 Flash. Any platform, any channel.

OpenClaw にインスパイアされた、Google Agent Development Kit (ADK) ベースのパーソナルAIエージェントです。

## 特徴

- 🤖 **Gemini 3.0 Flash** - 最新の高速AIモデル
- 🔍 **Web検索** - DuckDuckGo API 経由
- 🌐 **ブラウザ自動化** - Playwright によるスクレイピング、スクリーンショット
- 💻 **シェル実行** - セキュリティポリシー付き安全なコマンド実行
- 📁 **ファイル操作** - 読み書き対応
- 🧠 **メモリシステム** - SQLite + ベクトル検索
- 💬 **マルチチャネル** - Telegram, Discord, WebSocket 対応
- 🔒 **セキュリティ** - 監査ログ、コマンドポリシー
- 🔌 **拡張可能** - スキルプラグインシステム
- 🐳 **Docker対応** - `docker compose` で簡単デプロイ

## アーキテクチャ

OpenClaw の本質的アーキテクチャを Python で再現:

```
boiled-claw/
├── Gateway (WebSocket制御プレーン: ws://127.0.0.1:18789)
├── Agents (Gemini 3.0 Flash ベース)
│   ├── Root Agent (メイン)
│   └── Sub Agents (Web, File, System, Memory, Browser)
├── Channels (12+ 統合可能)
│   ├── Telegram
│   ├── Discord
│   └── WebSocket
├── Memory (SQLite + ベクトル検索)
├── Security (監査ログ + ポリシー)
└── Skills (プラグイン拡張)
```

## セットアップ

### 1. 依存関係インストール

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

# オプション: ブラウザ自動化
pip install -e ".[browser]"
playwright install chromium

# オプション: 全機能
pip install -e ".[all]"
```

### 2. 環境変数設定

```bash
cp .env.example .env
# .env を編集して GOOGLE_API_KEY を設定
```

Google API Key は [Google AI Studio](https://aistudio.google.com/apikey) で取得できます。

### 3. 実行

#### CLIモード
```bash
python -m src.main cli
# または
boiled-claw
```

#### Webサーバーモード (WebSocket Gateway)
```bash
python -m src.main web
# WebSocket endpoint: ws://127.0.0.1:18789/ws/{user_id}
```

#### チャネルモード (Telegram, Discord)
```bash
# .env にチャネルトークンを設定してから
python -m src.main channels
```

### Docker で実行

```bash
# .env ファイル作成
cp .env.example .env
# GOOGLE_API_KEY を設定

# 起動
docker compose up -d --build

# ログ確認
docker compose logs -f app

# 停止
docker compose down
```

ブラウザ自動化 (Playwright) をコンテナに含めたい場合:

```bash
docker compose build --build-arg INSTALL_BROWSER=true
docker compose up -d
```

## プロジェクト構造

```
boiled-claw/
├── src/
│   ├── agents/
│   │   ├── root_agent.py       # メインエージェント (gemini-3.0-flash)
│   │   ├── sub_agents.py       # サブエージェント定義
│   │   └── model_config.py     # モデル設定管理
│   ├── gateway/
│   │   ├── server.py           # WebSocketゲートウェイサーバー
│   │   ├── session_manager.py  # セッション管理
│   │   └── router.py           # メッセージルーティング
│   ├── tools/
│   │   ├── web_search.py       # Web検索
│   │   ├── shell.py            # シェル実行
│   │   ├── file_manager.py     # ファイル操作
│   │   ├── browser.py          # ブラウザ自動化
│   │   └── memory.py           # メモリツール
│   ├── channels/
│   │   ├── base.py             # チャネル基底クラス
│   │   ├── registry.py         # チャネルレジストリ
│   │   ├── telegram.py         # Telegram統合
│   │   └── discord_ch.py       # Discord統合
│   ├── memory/
│   │   └── (メモリストア実装)
│   ├── security/
│   │   ├── audit.py            # 監査ログ
│   │   └── policy.py           # セキュリティポリシー
│   ├── config/
│   │   ├── settings.py         # Pydantic設定
│   │   └── schema.py           # 設定スキーマ
│   ├── skills/
│   │   ├── loader.py           # スキルローダー
│   │   └── base.py             # スキル基底クラス
│   └── main.py                 # エントリーポイント
├── Dockerfile
├── docker-compose.yml
├── pyproject.toml
├── .env.example
├── tests/
└── README.md
```

## 使い方

### CLIで使う

```bash
$ python -m src.main cli

You: Pythonの最新ニュースを検索して

boiled-claw 🦀 [Web検索を実行...]
Python 3.12がリリースされました...
```

### WebSocket経由で使う

```python
import asyncio
import websockets
import json

async def chat():
    uri = "ws://127.0.0.1:18789/ws/my_user_id"
    async with websockets.connect(uri) as websocket:
        # メッセージ送信
        await websocket.send(json.dumps({
            "type": "message",
            "message": "Hello, boiled-claw!"
        }))

        # レスポンス受信
        response = await websocket.recv()
        print(json.loads(response))

asyncio.run(chat())
```

### Telegramで使う

1. BotFather で Telegram Bot を作成
2. `.env` に `TELEGRAM_BOT_TOKEN` を設定
3. `python -m src.main channels` で起動
4. Telegram で Bot にメッセージ送信

## 機能一覧

### ツール

- **web_search** - DuckDuckGo API で Web 検索
- **browser_navigate** - URL に移動
- **browser_screenshot** - スクリーンショット取得
- **browser_extract_text** - テキスト抽出
- **run_shell** - シェルコマンド実行
- **read_file** - ファイル読み込み
- **write_file** - ファイル書き込み
- **memory_store** - メモリに保存
- **memory_search** - メモリから検索

### セキュリティ

- 監査ログ (全操作を記録)
- コマンドブロックリスト
- パスアクセス制御
- 秘密情報検出

### チャネル

- Telegram (python-telegram-bot)
- Discord (discord.py)
- WebSocket (FastAPI)

## 開発

### テスト

```bash
pytest tests/
```

### Lint

```bash
ruff check src/
```

## ロードマップ

- [x] 基本エージェント構造 (Google ADK)
- [x] Gemini 3.0 Flash モデル
- [x] Web検索ツール
- [x] シェル実行ツール
- [x] ファイル操作ツール
- [x] ブラウザ自動化 (Playwright)
- [x] メモリシステム (SQLite + ベクトル検索)
- [x] WebSocketゲートウェイ
- [x] Telegram チャネル
- [x] Discord チャネル
- [x] セキュリティ (監査ログ + ポリシー)
- [x] Docker 対応
- [x] マルチエージェント (サブエージェント)
- [x] スキルプラグインシステム
- [ ] Redis セッション
- [ ] Slack チャネル
- [ ] WhatsApp チャネル
- [ ] Canvas (ビジュアルワークスペース)
- [ ] 音声インターフェース

## 参考

- [OpenClaw](https://github.com/openclaw/openclaw) - インスピレーション元 (1,500-2,000 ファイルの大規模TypeScriptプロジェクト)
- [Google ADK](https://google.github.io/adk-docs/) - エージェントフレームワーク

## ライセンス

MIT
