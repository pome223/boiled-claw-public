# 🦀 boiled-claw

> Your personal AI agent powered by Google ADK and Gemini 3.0 Flash. Any platform, any channel.

OpenClaw にインスパイアされた、Google Agent Development Kit (ADK) ベースのパーソナルAIエージェントです。

## 特徴

- 🤖 **Gemini 3.0 Flash** - 最新の高速AIモデル
- 🔍 **Web検索** - DuckDuckGo API 経由
- 🌐 **ブラウザ自動化** - Playwright によるスクレイピング、スクリーンショット
- 💻 **シェル実行** - セキュリティポリシー付き安全なコマンド実行
- 📁 **ファイル操作** - 読み書き対応
- 🧩 **Host Bridge** - host OS 上の shell / file / browser を別プロセスで実行
- 🧠 **メモリシステム** - SQLite + ベクトル検索
- 💬 **マルチチャネル** - Telegram, Discord, WebSocket 対応
- 🤝 **マルチエージェント委譲** - ADK sub_agents + AgentTool + sessions_spawn
- 🔧 **動的エージェント生成** - 実行時に MCP サーバーをアタッチしてエージェントを生成
- 🧭 **Typed Gateway Protocol** - `chat.send` / `chat.history` / `chat.abort` / `tools.approval`
- 📝 **永続 transcript** - SQLite-backed session history を Gateway が保持
- ⏰ **Cron platform** - system event 連動、delivery target、retry をサポート
- 🔌 **MCP サポート** - SSE / HTTP / stdio 接続に対応したサンプル MCP サーバー同梱
- 🔒 **セキュリティ** - 監査ログ、コマンドポリシー、tool approvals
- 📦 **拡張可能** - スキルプラグインシステム
- 🐳 **Docker対応** - `docker compose` で簡単デプロイ

## アーキテクチャ

OpenClaw の control plane / execution plane 分離に影響を受けつつ、boiled-claw では次の 3 層で構成しています。

- **Gateway (Docker / control plane)**: routing、session、transcript、cron、approvals、UI event stream
- **Host Bridge (host OS / execution plane)**: shell、file、browser を host 上の別プロセスで実行
- **Desktop Bridge (host OS / desktop capability plane)**: GUI automation と Accessibility 向けの runtime

```
boiled-claw/
├── Gateway
│   ├── typed WS / HTTP protocol
│   ├── transcript / cron / approvals
│   └── routing_agent / root_agent / control_loop
├── Host Bridge
│   ├── host.shell.run
│   ├── host.file.read / write / list
│   └── host.browser.navigate / extract_text / screenshot
├── Desktop Bridge
│   ├── desktop.view.*
│   └── desktop.control.*   (high-risk, still expanding)
├── MCP Servers
│   └── sample / host_bridge / desktop_bridge
└── Skills / Memory / Channels / Security
```

## セットアップ

Docker ベースの運用と開発を前提にしています。ホスト側の `.venv` は使いません。

### 1. 前提条件

- Docker
- Docker Compose v2

### 2. 環境変数設定

```bash
cp .env.example .env
# .env を編集して GOOGLE_API_KEY を設定
# 必要に応じて Gateway auth も設定:
# GATEWAY_API_KEY=change-me
# GATEWAY_AUTH_USER_HEADER=X-Auth-User
```

Google API Key は [Google AI Studio](https://aistudio.google.com/apikey) で取得できます。

`GATEWAY_API_KEY` を設定すると Gateway の HTTP / WebSocket API に認証が掛かります。
さらに `GATEWAY_AUTH_USER_HEADER` を設定した場合、effective `user_id` はその trusted header
から解決され、path や body に含まれる `user_id` では上書きできません。`GATEWAY_AUTH_USER_HEADER`
を未設定のまま shared API key を使う場合、認証済みリクエストは単一の shared principal に束ねられます。

### 3. Gateway を起動

```bash
# Gateway のみ起動
docker compose up -d --build boiled-claw-gateway

# Gateway + サンプル MCP サーバーを起動
docker compose up -d --build boiled-claw-gateway boiled-claw-mcp-sample

# ログ確認
docker compose logs -f boiled-claw-gateway

# 停止
docker compose down
```

Gateway 起動後のエンドポイント:

- Web UI: `http://127.0.0.1:18789/chat`
- WebSocket endpoint: `ws://127.0.0.1:18789/ws/{user_id}`
- Protocol schema: `http://127.0.0.1:18789/protocol`

### 4. コンテナから使う

#### CLIモード

```bash
docker compose --profile cli run --rm boiled-claw-cli cli
```

#### チャネルモード (Telegram, Discord)

```bash
# .env にチャネルトークンを設定してから
docker compose run --rm boiled-claw-gateway python -m src.main channels
```

#### 開発コマンド

```bash
# 単体テスト
docker compose --profile dev run --rm boiled-claw-dev pytest tests/ -m "not e2e"

# E2E テスト
docker compose --profile dev run --rm boiled-claw-dev pytest tests/test_e2e.py -v -m e2e

# Lint
docker compose --profile dev run --rm boiled-claw-dev ruff check src/
```

`boiled-claw-dev` は Docker ネットワーク内で `GATEWAY_URL=http://boiled-claw-gateway:18789`
を使うため、E2E テストもホストの Python 環境に依存しません。

#### ブラウザ自動化をコンテナに含める

```bash
docker compose build --build-arg INSTALL_BROWSER=true boiled-claw-gateway
docker compose up -d boiled-claw-gateway
```

### 5. Host Bridge を host OS で起動する

Gateway を Docker に残したまま host shell / file / browser を使う場合は、
Host Bridge を **Docker 外の別プロセス** として起動します。

この standalone bridge 自体には `GOOGLE_API_KEY` は不要です。

```bash
# SSE で起動
python -m src.main host-bridge --host 127.0.0.1 --port 8766

# または console script
boiled-claw-host-bridge --sse --host 127.0.0.1 --port 8766
```

`.env` には次を設定します。

```bash
HOST_BRIDGE_ENABLED=true
HOST_BRIDGE_URL=http://127.0.0.1:8766/sse
```

Playwright を Host Bridge 側で使う場合は、host 側 Python 環境に browser extras を入れておきます。

```bash
pip install -e '.[browser]'
playwright install
```

### 6. Desktop Bridge

Desktop Bridge は `DesktopClient` を呼ぶ thin adapter です。
現時点では macOS 向けの `pyobjc` 実装が入り、
`frontmost_app` / `windows` / `screenshot` / `ax_find` / `ax_snapshot` に加えて、
`launch_app` / `focus_window` / `click` / `type` / `hotkey` / `drag` まで扱えます。
`click` と `type` は座標指定だけでなく、Accessibility selector を使った targeting にも対応します。
こちらも standalone bridge として起動でき、`GOOGLE_API_KEY` は不要です。

```bash
pip install -e '.[desktop]'
```

```bash
python -m src.main desktop-bridge --host 127.0.0.1 --port 8767

# または console script
boiled-claw-desktop-bridge --sse --host 127.0.0.1 --port 8767
```

Gateway から Desktop Bridge を使うときは `.env` に次を設定します。

```bash
DESKTOP_BRIDGE_ENABLED=true
DESKTOP_BRIDGE_URL=http://127.0.0.1:8767/sse
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
│   │   ├── protocol.py         # Typed Gateway Protocol v1
│   │   ├── transcript.py       # 永続 transcript / history
│   │   ├── session_manager.py  # セッション管理
│   │   └── router.py           # メッセージルーティング
│   ├── bridges/
│   │   ├── host_bridge_schema.py     # Host Bridge contract
│   │   ├── host_bridge_client.py     # Host Bridge MCP client
│   │   ├── host_bridge_exec.py       # Host Bridge 共通実行ヘルパー
│   │   └── desktop_bridge_schema.py  # Desktop Bridge contract
│   ├── tools/
│   │   ├── web_search.py       # Web検索
│   │   ├── shell.py            # シェル実行
│   │   ├── file_manager.py     # ファイル操作
│   │   ├── context.py          # ToolContext 共通解決
│   │   ├── browser.py          # ブラウザ自動化
│   │   ├── memory.py           # メモリツール
│   │   └── subagents.py        # サブエージェント・動的エージェント管理
│   ├── mcp_servers/
│   │   ├── sample_server.py         # サンプル MCP サーバー
│   │   ├── host_bridge_server.py    # Host Bridge MCP server
│   │   └── desktop_bridge_server.py # Desktop Bridge adapter server
│   ├── channels/
│   │   ├── base.py             # チャネル基底クラス
│   │   ├── registry.py         # チャネルレジストリ
│   │   ├── telegram.py         # Telegram統合
│   │   └── discord_ch.py       # Discord統合
│   ├── memory/
│   │   └── (メモリストア実装)
│   ├── security/
│   │   ├── audit.py            # 監査ログ
│   │   ├── policy.py           # コマンド/パス セキュリティポリシー
│   │   └── tool_policy.py      # tool approvals / per-agent policy
│   ├── config/
│   │   ├── settings.py         # Pydantic設定
│   │   └── schema.py           # 設定スキーマ
│   ├── skills/
│   │   ├── loader.py           # スキルローダー
│   │   └── base.py             # スキル基底クラス
│   └── main.py                 # エントリーポイント
├── tests/
│   ├── test_sample_mcp_server.py  # MCP サーバーテスト
│   └── ...
├── Dockerfile
├── docker-compose.yml
├── pyproject.toml
├── .env.example
└── README.md
```

## 使い方

### CLIで使う

```bash
$ docker compose --profile cli run --rm boiled-claw-cli cli

You: Pythonの最新ニュースを検索して

boiled-claw 🦀 [Web検索を実行...]
Python 3.12がリリースされました...
```

### WebSocket経由で使う

```python
import asyncio
import json
import websockets

async def chat():
    uri = "ws://127.0.0.1:18789/ws/my_user_id"
    async with websockets.connect(uri) as websocket:
        connected = json.loads(await websocket.recv())
        print("connected:", connected)

        await websocket.send(json.dumps({
            "event": "chat.send",
            "text": "Hello, boiled-claw!",
            "request_id": "demo-1"
        }))

        while True:
            event = json.loads(await websocket.recv())
            print(event)
            if event["event"] == "chat.done":
                break

asyncio.run(chat())
```

クライアントから送る主なイベント:

- `chat.send`
- `chat.inject`
- `chat.abort`
- `chat.history`
- `presence.ping`
- `tools.approval`

サーバーから返る主なイベント:

- `connected`
- `chat.done`
- `chat.history`
- `system.event`
- `health.tick`
- `cron.update`
- `tools.approval_request`

イベント schema は `GET /protocol` で取得できます。

### HTTP API（curl）で使う

```bash
curl -sS -X POST http://127.0.0.1:18789/agent/run \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${GATEWAY_API_KEY}" \
  -d '{
    "user_id": "curl_user",
    "message": "NVIDIAの最新ニュースを3つ教えて"
  }'
```

`GATEWAY_API_KEY` を設定していない場合は `Authorization` ヘッダは不要です。
`session_id` を指定すれば同一会話を継続できます。

### Gateway 認証と `user_id`

- 認証無効時: HTTP body / WebSocket path の `user_id` をそのまま使います。
- `GATEWAY_API_KEY` のみ設定時: 認証済みリクエストは single shared principal に束ねられます。
- `GATEWAY_API_KEY` + `GATEWAY_AUTH_USER_HEADER` 設定時: effective `user_id` は trusted header から解決されます。

つまり、認証有効時は path/body の `user_id` を transcript ownership の境界として信用しません。
reverse proxy や API gateway で認証済みユーザー ID を `GATEWAY_AUTH_USER_HEADER` に流す構成を想定しています。

### Gateway の主要エンドポイント

- `GET /protocol` - typed protocol schema
- `GET /sessions/{user_id}` - セッション一覧
- `GET /sessions/{user_id}/{session_id}/history` - transcript history
- `GET /transcript/sessions?user_id=...` - transcript-backed session summaries
- `POST /cron` / `GET /cron` - cron platform
- `GET /tools/policy` - tool policy 一覧
- `GET /tools/approvals` - pending approval 一覧

### Telegramで使う

1. BotFather で Telegram Bot を作成
2. `.env` に `TELEGRAM_BOT_TOKEN` を設定
3. `docker compose run --rm boiled-claw-gateway python -m src.main channels` で起動
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
- **agents_list** - 利用可能なサブエージェント一覧
- **sessions_spawn** - サブエージェントをバックグラウンド起動
- **sessions_spawn_dynamic** - MCP サーバー付き動的エージェントを生成・起動
- **subagents_list** - サブエージェント実行の状態確認
- **subagents_steer** - mode=session のサブエージェントへ追加入力
- **subagents_kill** - サブエージェント実行停止
- **skill_list** - ロード済みスキル一覧を取得
- **skill_execute** - 指定スキルを実行
- **skill_spawn** - スキル内容を instruction にして動的 Agent を起動

### 動的エージェント生成 (sessions_spawn_dynamic)

実行時にシステムプロンプトと MCP サーバーを指定して、カスタムエージェントを動的に生成できます。

```bash
# エージェントを起動（例: サンプル MCP サーバーをアタッチ）
curl -sS -X POST http://127.0.0.1:18789/agent/run \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "my_user",
    "message": "sessions_spawn_dynamic でエージェントを起動して。instruction=\"あなたは計算エージェントです\"、mcp_servers=[{\"type\":\"sse\",\"url\":\"http://boiled-claw-mcp-sample:8765/sse\"}]、task=\"100 + 200 を計算して\""
  }'

# 実行結果を確認
curl http://127.0.0.1:18789/subagents/{session_id}
```

**MCP 接続タイプ:**

| type | 説明 | 設定例 |
|------|------|--------|
| `sse` | SSE 接続 | `{"type": "sse", "url": "http://..."}` |
| `http` | Streamable HTTP 接続 | `{"type": "http", "url": "http://..."}` |
| `stdio` | サブプロセス起動 | `{"type": "stdio", "command": "npx", "args": [...]}` |

### サンプル MCP サーバー

`src/mcp_servers/sample_server.py` に FastMCP ベースのサンプルサーバーを同梱しています。

**提供ツール:**

| ツール | 説明 |
|--------|------|
| `echo(text)` | テキストをそのまま返す |
| `add(a, b)` | 2数値の加算 |
| `current_time()` | 現在日時を ISO 8601 で返す |
| `reverse_text(text)` | テキストを逆順にする |

**起動方法:**

```bash
# SSE モード（docker-compose.yml に定義済み）
docker compose up -d boiled-claw-mcp-sample
# → http://localhost:8765/sse でアクセス可能
```

Docker ネットワーク内からは `http://boiled-claw-mcp-sample:8765/sse` で接続できます。

### Skills の使い方

- `skills/<name>/SKILL.md` を追加すると起動時に自動ロードされます（OpenClaw 形式）。
- 互換性のために `skills/*.py` の旧形式も引き続きロードされます。
- ゲートウェイ起動後に `GET /skills` でロード状況を確認できます。
- `skill_execute` はスキル内容の確認・実行に使います。
- `skill_spawn` はスキル内容を dynamic agent の instruction として使い、タスク実行を委譲します。
- サンプルとして `skills/coding-agent/SKILL.md` と `skills/e2e-test/SKILL.md` を同梱しています。

### セキュリティ

- 監査ログ (全操作を記録)
- コマンドブロックリスト
- パスアクセス制御
- 秘密情報検出
- per-agent tool policy
- tool approval request / resolve (`tools.approval_request`, `tools.approval`)
- Gateway API key + trusted identity header による transcript ownership 保護

### チャネル

- Telegram (python-telegram-bot)
- Discord (discord.py)
- WebSocket (FastAPI)

## 開発

### テスト

```bash
docker compose --profile dev run --rm boiled-claw-dev pytest tests/ -m "not e2e"
```

### Lint

```bash
docker compose --profile dev run --rm boiled-claw-dev ruff check src/
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
- [x] Typed Gateway Protocol v1
- [x] Gateway-owned transcript / history persistence
- [x] Cron platform (delivery target / retry / system events)
- [x] Tool security / approvals
- [x] Telegram チャネル
- [x] Discord チャネル
- [x] セキュリティ (監査ログ + ポリシー)
- [x] Docker 対応
- [x] マルチエージェント (サブエージェント)
- [x] スキルプラグインシステム
- [x] 動的エージェント生成 (sessions_spawn_dynamic)
- [x] MCP サポート (SSE / HTTP / stdio) + サンプルサーバー
- [ ] Redis セッション
- [ ] Slack チャネル
- [ ] WhatsApp チャネル
- [ ] Canvas (ビジュアルワークスペース)
- [ ] 音声インターフェース

## 参考

- [OpenClaw](https://github.com/openclaw/openclaw) - インスピレーション元 (1,500-2,000 ファイルの大規模TypeScriptプロジェクト)
- [Google ADK](https://google.github.io/adk-docs/) - エージェントフレームワーク
- [MCP (Model Context Protocol)](https://modelcontextprotocol.io/) - ツール接続プロトコル

## ライセンス

MIT
