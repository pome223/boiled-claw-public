"""
Root Agent - boiled-claw のメインエージェント
Google ADK を使ったパーソナルAIアシスタント
"""

from google.adk.agents import Agent
from src.tools.web_search import web_search
from src.tools.shell import run_shell
from src.tools.file_manager import read_file, write_file

root_agent = Agent(
    name="boiled_claw",
    model="gemini-2.0-flash",
    description=(
        "boiled-claw: Your personal AI agent. "
        "Helps you with tasks across any platform."
    ),
    instruction="""
あなたは boiled-claw、ユーザーの個人AIアシスタントです。

## あなたの能力
- Webの情報を検索・取得する
- シェルコマンドを実行する
- ファイルを読み書きする
- 複雑なタスクを段階的に実行する

## 行動原則
- ユーザーのリクエストを明確に理解してから行動する
- 不明な点は確認する
- 実行した結果を簡潔に報告する
- 日本語と英語の両方に対応する

## 安全性
- 危険なコマンドや操作は実行前に必ず確認を取る
- 個人情報や機密情報は慎重に扱う
""",
    tools=[
        web_search,
        run_shell,
        read_file,
        write_file,
    ],
)
