# boiled-claw アーキテクチャ

OpenClaw にインスパイアされた、Google ADK ベースの browser-first closed-loop agent system。computer use、verification-driven recovery、future self-improvement、physical-ready adapters を一つの制御プレーンにまとめる。

## 概要

boiled-claw は、OpenClaw の control plane / execution plane separation を Python で再構成しつつ、2026 年のエージェント設計で重要な 4 つの軸をまとめたものです。

- browser-first computer use
- desktop fallback と policy-bounded control loop
- trajectory-aware verify / repair / future self-improvement
- simulator / robotics runtime に伸ばせる physical-ready adapter surface

このドキュメントは現在実装されている構成を中心に説明します。physical AI 方向は forward path であり、現時点でフル robotics stack を提供するものではありません。

### 主要コンポーネント

```
┌─────────────────────────────────────────────────────────────┐
│                        boiled-claw                          │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐     │
│  │   Gateway    │  │   Channels   │  │    Agents    │     │
│  │  (WebSocket) │  │ (Telegram,   │  │    (ADK)     │     │
│  │              │  │  Discord)    │  │              │     │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘     │
│         │                 │                 │               │
│         └─────────────────┴─────────────────┘               │
│                           │                                 │
│         ┌─────────────────┴─────────────────┐               │
│         │                                   │               │
│  ┌──────▼───────┐  ┌──────────────┐  ┌─────▼──────┐       │
│  │   Security   │  │    Memory    │  │   Skills   │       │
│  │  (Audit +    │  │  (SQLite +   │  │  (Plugins) │       │
│  │   Policy)    │  │   Vector)    │  │            │       │
│  └──────────────┘  └──────────────┘  └────────────┘       │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

## ディレクトリ構造

```
src/
├── agents/                  # エージェント定義
│   ├── root_agent.py        # メインエージェント (default model)
│   ├── sub_agents.py        # サブエージェント (Web, File, System, Memory, Browser)
│   └── model_config.py      # モデル設定
│
├── gateway/                 # WebSocketゲートウェイ
│   ├── server.py            # FastAPI サーバー (ws://127.0.0.1:18789)
│   ├── session_manager.py   # セッション管理
│   └── router.py            # メッセージルーティング
│
├── channels/                # チャネル統合
│   ├── base.py              # チャネル基底クラス
│   ├── registry.py          # チャネルレジストリ
│   ├── telegram.py          # Telegram統合
│   └── discord_ch.py        # Discord統合
│
├── tools/                   # ツール (エージェントが使用)
│   ├── web_search.py        # Web検索 (DuckDuckGo)
│   ├── browser.py           # ブラウザ自動化 (Playwright)
│   ├── shell.py             # シェル実行
│   ├── file_manager.py      # ファイル操作
│   └── memory.py            # メモリツール
│
├── memory/                  # メモリシステム
│   └── (MemoryStore実装 in tools/memory.py)
│
├── security/                # セキュリティ
│   ├── audit.py             # 監査ログ
│   └── policy.py            # セキュリティポリシー
│
├── config/                  # 設定
│   ├── settings.py          # Pydantic Settings
│   └── schema.py            # 設定スキーマ
│
├── skills/                  # スキルプラグインシステム
│   ├── base.py              # スキル基底クラス
│   └── loader.py            # スキルローダー
│
└── main.py                  # エントリーポイント (CLI / Web / Channels)
```

## コンポーネント詳細

### 1. Agents (エージェント)

#### Root Agent
- **モデル**: gemini-3-flash-preview
- **役割**: メインエージェント、全ツールにアクセス可能
- **ツール**: web_search, browser, shell, file, memory, skills, subagents
- **委譲方式**: ADK `sub_agents` + `AgentTool` + `TransferToAgentTool`

#### Sub Agents
- **web_agent**: Web検索とブラウジング専門
- **file_agent**: ファイル操作とコード解析専門
- **system_agent**: システムコマンド実行専門
- **memory_agent**: メモリ管理専門
- **browser_agent**: ブラウザ自動化専門
- **オーケストレーション**: `sessions_spawn` / `subagents_list` / `subagents_steer` / `subagents_kill`

### 2. Gateway (ゲートウェイ)

#### WebSocket Server
- **エンドポイント**: `ws://127.0.0.1:18789/ws/{user_id}`
- **プロトコル**: JSON over WebSocket
- **機能**:
  - セッション管理
  - メッセージルーティング
  - リアルタイム双方向通信

#### Session Manager
- InMemorySessionService ベース
- タイムアウト管理 (デフォルト: 1時間)
- メタデータ保存

#### Message Router
- チャネル別ルーティング
- ブロードキャスト機能
- ハンドラー登録システム

### 3. Channels (チャネル)

#### Base Channel
- 抽象基底クラス
- 共通インターフェース:
  - `start()` - チャネル開始
  - `stop()` - チャネル停止
  - `send_message()` - メッセージ送信
  - `handle_incoming_message()` - メッセージ処理

#### Telegram Channel
- python-telegram-bot 使用
- コマンドハンドラー (`/start`)
- メッセージハンドラー

#### Discord Channel
- discord.py 使用
- イベントハンドラー (`on_message`, `on_ready`)
- コマンドサポート

#### Channel Registry
- チャネル登録・管理
- 一括開始/停止

### 4. Tools (ツール)

#### web_search
- DuckDuckGo Instant Answer API
- API key 不要
- 検索結果のパース

#### browser (Playwright)
- `browser_navigate()` - URL移動
- `browser_screenshot()` - スクリーンショット
- `browser_extract_text()` - テキスト抽出
- `browser_click()` - クリック
- `browser_fill()` - フォーム入力

#### shell
- セキュリティチェック付きコマンド実行
- ブラックリスト方式
- タイムアウト設定

#### file_manager
- `read_file()` - ファイル読み込み
- `write_file()` - ファイル書き込み
- パーミッションチェック

#### memory
- `memory_store()` - メモリ保存
- `memory_search()` - メモリ検索
- タグベース検索
- コサイン類似度によるベクトル検索

### 5. Memory (メモリシステム)

#### MemoryStore
- **バックエンド**: SQLite
- **検索**: テキスト検索 + ベクトル検索
- **機能**:
  - タグベース分類
  - メタデータ保存
  - タイムスタンプ管理
  - 類似度検索 (Google埋め込みモデル + コサイン類似度)

### 6. Security (セキュリティ)

#### Audit Logger
- **形式**: JSON Lines
- **イベント**:
  - SHELL_COMMAND - シェルコマンド実行
  - FILE_READ/WRITE - ファイル操作
  - AGENT_MESSAGE - エージェントメッセージ
  - SESSION_START/END - セッション開始/終了
  - ERROR - エラー

#### Security Policy
- **コマンドポリシー**:
  - ブロックリスト (rm -rf, sudo rm, etc.)
  - 許可リスト (オプション)
- **パスポリシー**:
  - システムディレクトリ保護
  - 秘密鍵検出
- **コンテンツ検証**:
  - 秘密情報パターン検出

### 7. Skills (スキルプラグインシステム)

#### BaseSkill
- 抽象基底クラス
- メタデータ定義
- `execute()` メソッド
- `on_load()` / `on_unload()` フック

#### SkillLoader
- 動的ロード
- ディレクトリスキャン
- リロード機能

#### SkillRegistry
- スキル登録・管理
- 有効/無効切り替え

### 8. Config (設定)

#### Settings (Pydantic)
- 環境変数からの読み込み
- バリデーション
- デフォルト値
- 型安全性

#### Schema
- ChannelConfig
- ModelConfig
- SessionConfig
- SecurityConfig
- AppConfig

## データフロー

### 1. CLI モード

```
User Input
    ↓
main.py (run_cli)
    ↓
Runner.run_async()
    ↓
root_agent (configured default model)
    ↓
Tools (web_search, shell, etc.)
    ↓
Response to User
```

### 2. Web モード (WebSocket)

```
WebSocket Client
    ↓
Gateway Server (ws://127.0.0.1:18789/ws/{user_id})
    ↓
Session Manager
    ↓
Runner.run_async()
    ↓
root_agent
    ↓
Tools
    ↓
WebSocket Response
```

### 3. Channel モード

```
Telegram/Discord Message
    ↓
Channel Handler
    ↓
Message Handler (handle_message)
    ↓
Runner.run_async()
    ↓
root_agent
    ↓
Tools
    ↓
Channel Response
```

## セキュリティフロー

```
User Request
    ↓
Security Policy Check
    ├─ Blocked → Audit Log → Error Response
    └─ Allowed
        ↓
    Execute Tool
        ↓
    Audit Log (Success/Failure)
        ↓
    Response
```

## 拡張性

### 新しいツールの追加

1. `src/tools/` に新しいツールファイル作成
2. 非同期関数として実装
3. `root_agent.py` の `tools` リストに追加

### 新しいチャネルの追加

1. `src/channels/` に新しいチャネルファイル作成
2. `BaseChannel` を継承
3. `start()`, `stop()`, `send_message()` を実装
4. Channel Registry に登録

### 新しいスキルの追加

1. `skills/` ディレクトリに `.py` ファイル作成
2. `BaseSkill` を継承
3. `get_metadata()`, `execute()` を実装
4. 自動ロード

## パフォーマンス

- **モデル**: configurable default model (高速寄り設定)
- **非同期**: 全 I/O 操作は async/await
- **セッション**: InMemory (将来: Redis)
- **メモリ**: SQLite (軽量)

## スケーラビリティ

### 現在
- InMemory Session
- SQLite Memory
- 単一プロセス

### 将来
- Redis Session (分散セッション)
- Vector DB (大規模メモリ)
- マルチプロセス/ワーカー

## 開発ガイドライン

### コーディング規約
- **フォーマッター**: Black / Ruff
- **型ヒント**: 必須
- **Docstring**: Google スタイル
- **非同期**: async/await 優先

### テスト
- **フレームワーク**: pytest
- **カバレッジ**: 目標 80%+
- **単体テスト**: 各モジュール
- **統合テスト**: エンドツーエンド

## デプロイ

### Docker
```bash
docker compose up -d --build
```

Docker ベースの運用を前提とし、起動・テスト・lint は `docker compose` に集約します。

## モニタリング

- **監査ログ**: `data/audit.log`
- **アプリケーションログ**: stdout/stderr
- **メトリクス**: (将来実装)

## トラブルシューティング

### よくある問題

1. **GOOGLE_API_KEY not set**
   - `.env` ファイルに API キーを設定

2. **Playwright not installed**
   - `pip install playwright && playwright install chromium`

3. **Channel token error**
   - `.env` にチャネルトークンを設定

## まとめ

boiled-claw は、OpenClaw の思想を継承しつつ、Python と Google ADK の利点を活かした実装です。

- **シンプル**: 2,700行のコード
- **パワフル**: 12+ チャネル対応可能
- **拡張可能**: プラグインシステム
- **安全**: セキュリティポリシー + 監査ログ
- **高速**: 軽量な default model を前提
