# 🦀 boiled-claw

> Your personal AI agent powered by Google ADK. Any platform, any channel.

OpenClaw にインスパイアされた、Google Agent Development Kit (ADK) ベースのパーソナルAIエージェントです。

## 特徴

- 🤖 **Google ADK** を使ったマルチエージェント対応
- 🔍 **Web検索** - DuckDuckGo API 経由
- 💻 **シェル実行** - 安全なコマンド実行
- 📁 **ファイル操作** - 読み書き対応
- 💬 **マルチチャネル** - Telegram, Discord, Slack 対応予定
- 🌐 **CLI / Web UI** 両対応予定

## セットアップ

### 1. 依存関係インストール

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

### 2. 環境変数設定

```bash
cp .env.example .env
# .env を編集して GOOGLE_API_KEY を設定
```

Google API Key は [Google AI Studio](https://aistudio.google.com/apikey) で取得できます。

### 3. 実行

```bash
# CLIモード
python -m src.main

# または
boiled-claw
```

## プロジェクト構造

```
boiled-claw/
├── src/
│   ├── agents/          # ADK エージェント定義
│   │   └── root_agent.py
│   ├── tools/           # カスタムツール
│   │   ├── web_search.py
│   │   ├── shell.py
│   │   └── file_manager.py
│   ├── channels/        # チャネル統合 (Telegram, Discord, etc.)
│   ├── skills/          # スキルプラグイン
│   └── main.py          # エントリーポイント
├── config/              # 設定ファイル
├── tests/               # テスト
├── .env.example
└── pyproject.toml
```

## ロードマップ

- [x] 基本エージェント構造 (Google ADK)
- [x] Web検索ツール
- [x] シェル実行ツール
- [x] ファイル操作ツール
- [ ] Telegram チャネル
- [ ] Discord チャネル
- [ ] Web UI (FastAPI + WebSocket)
- [ ] マルチエージェント (サブエージェント)
- [ ] スキルプラグインシステム
- [ ] 音声インターフェース

## 参考

- [OpenClaw](https://github.com/openclaw/openclaw) - インスピレーション元
- [Google ADK](https://google.github.io/adk-docs/) - エージェントフレームワーク
