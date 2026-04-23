# boiled-claw アーキテクチャ

OpenClaw にインスパイアされた、Google ADK ベースの browser-first closed-loop agent system。computer use、verification-driven recovery、future self-improvement、physical-ready adapters を一つの制御プレーンにまとめる。

## 概要

boiled-claw は、OpenClaw の control plane / execution plane separation を Python で再構成しつつ、2026 年のエージェント設計で重要な 4 つの軸をまとめたものです。

- browser-first computer use
- desktop fallback と policy-bounded control loop
- trajectory-aware verify / repair / future self-improvement
- simulator / robotics runtime に伸ばせる physical-ready adapter surface
- skills / bridges / browser-first tools をまとめる runtime substrate

このドキュメントは現在実装されている構成を中心に説明します。physical AI 方向は forward path であり、現時点でフル robotics stack を提供するものではありません。

## Next Development Spine

次に深掘りするべきなのは、Slack / Voice のような横展開ではなく、
**trajectory を中心にした eval / replay / repair / promotion の閉ループ強化**である。

要点は次の 6 つ。

1. `computer_*` の trajectory を first-class eval artifact にする
2. 失敗を taxonomy で分類し、verifier -> replay analysis -> normalized failure_type -> operator override の lifecycle を持たせる
3. benchmark を通過した修復を `approved_skill` / `capability_patch` / `approved_improvement_memory` / `policy_patch` に責務分離して昇格する
4. promotion を approval / audit / security eval で明示的に縛る
5. current-tab 優先 + desktop fallback の実用タスクを深掘りする
6. 同じ trajectory schema を simulation-first physical replay に再利用する

この spine は durable execution の代替ではなく、その上に乗る improvement layer として扱う。
ここでいう substrate は、既存の task store / replay / resume / approval queue / scheduler surfaces であり、
PR #83 の主役は workflow engine の完成ではなく、それらの durable artifact を
`trajectory -> eval -> failure classification -> promotion -> reuse` に接続すること。

この次段の substrate 実装は #84 / #85 / #87 で、
goal を durable `task_graph` に落とし、各 bounded job が `checkpoint` と
durable verifier verdict (`pass | fail | uncertain`) を残せるようにすること。
ここでも scheduler や full workflow engine までは広げず、persistent schema と
resume/retry の材料を先に揃える。
Phase 0 ではこれらは live scheduler state ではなく、eval run から組み立てられる
eval-derived substrate artifact として出している。

その次の slice が #86 / #88 / #89 / #90 で、
同じ eval-derived substrate の上に

- scheduler queue (`ready / blocked / waiting_for_approval / retry_later / periodic_check / completed`)
- failure-type driven recovery policy / recovery decision
- guardrail budget state
- durable human escalation record

を乗せること。
ここでもまだ background worker や distributed scheduler を実装するのではなく、
後続の runtime が読む durable contract を先に確定させる。

その次の #92 / #93 は、この contract を current-tab Google Sheets に縦に通す slice として扱う。
`evals/current_tab_google_sheets.yaml` は `long_running_vertical_slice` として複数 bounded job を評価し、
各 `run_jobs[]` が `trajectory_id / replay_reference / checkpoint / job_run / verifier_verdict` を持つ。
Control UI 側では `/tasks/{id}` の persisted report をそのまま描画し、
task graph / scheduler queues / approval waits / latest checkpoints / budget exhaustion を operator が確認できるようにする。

次の live mission runtime slice では、同じ contract を `ControlLoopSupervisor` の入口に上げる。
`mission_contract` は goal / constraints 互換入力から生成するか、HTTP API に直接渡せる。
supervisor は standalone scheduler daemon ではなく supervisor-owned live worker のままだが、
task artifact / durable_execution / task_graph metadata / scheduler queue metadata に同じ
`mission_contract` を永続化し、completion criteria / evidence requirements / abort conditions を
各 live task node から追えるようにする。

その次の #94 / #95 / #96 / #97 は、physical AI 側も同じ contract-first で進める slice として扱う。
ここで追加するのは live robotics runtime ではなく、simulation-first adapter が永続化する

- `mission_contract`
- `verifier_result`
- `telemetry_health`
- `action_envelope`
- `governor_decision`
- `replay_plan`

という durable artifact 群である。
`mission_contract` は objective / allowed_actions / forbidden_actions / abort_conditions /
completion_criteria を first-class に持ち、`verifier_result` は
`pass | fail | uncertain | unsafe` を telemetry health と evidence refs 付きで返す。
`action_envelope` と `governor_decision` は controller boundary と safety gate を明示し、
`replay_plan` は offline-only / benchmark-required / safety-regression-required /
operator-approved を contract として固定する。
この slice でも direct motor / thrust / attitude control や live self-modification は扱わない。

最小の first slice は、current-tab Google Sheets task を起点に
`trajectory -> normalized failure_type -> replay-linked report -> canary -> approved_improvement_memory reuse`
を通す形がよい。

Phase 0 の approved improvement memory reuse では、normalized `failure_type` と
trajectory hints を使って候補を引き、demo/search/eval report に
`reuse_memory_ids` / `reuse_policy` を載せ、trajectory 自体にも `reuse_trace`
を残す。reuse は `request.policy` / `observation.policy` / `trajectory.policy`
で opt-out できる。

Phase 0 の report では、少なくとも
`trajectory_id / verifier_result / failure_type / recommended_repair_targets / replay_reference`
が複数 run を並べた `run_jobs` として見えることを目標にする。

詳細な設計図は [architecture/trajectory-native-self-improving-runtime.md](architecture/trajectory-native-self-improving-runtime.md) を参照。

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
│             ┌──────────────────────┐                        │
│             │  Runtime Substrate   │                        │
│             │ resource_* /         │                        │
│             │ capability_*         │                        │
│             └──────────────────────┘                        │
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
- **ツール**: web_search, browser, shell, file, memory, skills, runtime substrate, subagents
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

runtime substrate により、従来は host bridge / desktop / current_tab / skills に分散していた surface を、`resource_list`, `resource_read`, `capability_list`, `capability_invoke` の 4 つで横断的に見られるようにしています。これにより Gateway の HTTP API と root agent が同じ canonical capability registry を共有します。

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
