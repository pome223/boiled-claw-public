"""
サブエージェント定義
OpenClaw のマルチエージェントアーキテクチャを参考
"""

from google.adk.agents import Agent
from src.tools.web_search import web_search
from src.tools.shell import run_shell
from src.tools.file_manager import read_file, write_file
from src.tools.browser import browser_navigate, browser_screenshot, browser_extract_text
from src.tools.desktop import (
    desktop_ax_find,
    desktop_ax_snapshot,
    desktop_control_click,
    desktop_control_drag,
    desktop_control_focus_window,
    desktop_control_hotkey,
    desktop_control_launch_app,
    desktop_control_scroll,
    desktop_control_type,
    desktop_runtime_clear_stop,
    desktop_runtime_status,
    desktop_runtime_stop,
    desktop_wait_element,
    desktop_wait_window,
    desktop_view_frontmost_app,
    desktop_view_screenshot,
    desktop_view_windows,
)
from src.tools.memory import memory_store, memory_search


# Web検索特化エージェント
web_agent = Agent(
    name="web_researcher",
    model="gemini-3-flash-preview",
    description="Web検索と情報収集を専門とするエージェント",
    instruction="""
あなたはWeb検索のスペシャリストです。

## 役割
- ユーザーの質問に対して最新の情報をWebから収集する
- 複数のソースから情報を集めて総合的な回答を作成する
- 信頼性の高い情報源を優先する

## 行動
1. 検索クエリを最適化する
2. Web検索を実行する
3. 結果を分析して要約する
4. 必要に応じて追加検索を行う
""",
    tools=[web_search, browser_navigate, browser_extract_text],
)


# ファイル操作特化エージェント
file_agent = Agent(
    name="file_manager",
    model="gemini-3-flash-preview",
    description="ファイル操作とコード解析を専門とするエージェント",
    instruction="""
あなたはファイル操作のスペシャリストです。

## 役割
- ファイルの読み書き、検索
- コードの解析とリファクタリング提案
- ファイル構造の整理

## 行動
1. ファイル操作の要件を明確にする
2. 安全性を確認してから実行する
3. 実行結果を検証する
4. 変更内容を明確に報告する
""",
    tools=[read_file, write_file, run_shell],
)


# システム操作特化エージェント
system_agent = Agent(
    name="system_operator",
    model="gemini-3-flash-preview",
    description="システムコマンド実行とタスク自動化を専門とするエージェント",
    instruction="""
あなたはシステム操作のスペシャリストです。

## 役割
- シェルコマンドの安全な実行
- システム情報の取得と分析
- タスクの自動化

## 安全原則
- 危険なコマンドは実行前に必ず確認する
- 実行内容を明確に説明する
- エラーハンドリングを徹底する

## 行動
1. コマンドの安全性を検証する
2. 実行前に目的を明確にする
3. 結果を分析して報告する
""",
    tools=[run_shell, read_file, write_file],
)


# メモリ管理エージェント
memory_agent = Agent(
    name="memory_keeper",
    model="gemini-3-flash-preview",
    description="会話履歴とメモリ管理を専門とするエージェント",
    instruction="""
あなたはメモリ管理のスペシャリストです。

## 役割
- 重要な情報を記憶に保存する
- 過去の会話や情報を検索する
- ユーザーの嗜好や文脈を記憶する

## 行動
1. 重要な情報を識別する
2. 適切なタグとメタデータで保存する
3. 関連情報を検索して提供する
4. メモリの整理と最適化を行う
""",
    tools=[memory_store, memory_search],
)


# ブラウザ自動化エージェント
browser_agent = Agent(
    name="browser_automator",
    model="gemini-3-flash-preview",
    description="ブラウザ自動化とスクレイピングを専門とするエージェント",
    instruction="""
あなたはブラウザ自動化のスペシャリストです。

## 役割
- Webページのナビゲーションとスクレイピング
- スクリーンショットの取得
- フォーム入力や自動操作

## 行動
1. 対象URLと目的を明確にする
2. ページの構造を理解する
3. 必要な情報を抽出する
4. 結果を構造化して返す

## 注意
- robots.txtとサイトポリシーを尊重する
- 過度なリクエストを避ける
- browser 系 tool が実行環境の問題で失敗した場合は、その失敗を明示して止まる
- Playwright 未導入や Host Bridge 未設定のときに、web_search や他エージェントへ自動フォールバックして「ブラウザで見た」とは言わない
""",
    tools=[browser_navigate, browser_screenshot, browser_extract_text, memory_store],
)


desktop_agent = Agent(
    name="desktop_operator",
    model="gemini-3-flash-preview",
    description="Desktop view/control を専門とするエージェント",
    instruction="""
あなたは desktop automation のスペシャリストです。

## 役割
- デスクトップの現在状態を観測する
- 前面アプリやウィンドウ構成を把握する
- 必要なときだけ GUI 入力を行う

## 原則
- まず view 系 tool で状況を確認する
- `desktop_wait_window` / `desktop_wait_element` を使って出現待ちをできる
- control 系 tool は最小限に使う
- 高リスク操作は承認が必要な場合がある
- できるだけ app / window / AX 情報に基づいて行動する
- `desktop_ax_find` を使って、full snapshot の前に対象要素の存在確認を行う
- 座標指定より、launch_app / focus_window / selector-aware click/type を優先する
- 制御不能になったら `desktop_runtime_stop` を最優先し、復帰前に `desktop_runtime_status` を確認する
""",
    tools=[
        desktop_view_windows,
        desktop_wait_window,
        desktop_view_frontmost_app,
        desktop_view_screenshot,
        desktop_ax_find,
        desktop_wait_element,
        desktop_ax_snapshot,
        desktop_runtime_status,
        desktop_runtime_stop,
        desktop_runtime_clear_stop,
        desktop_control_click,
        desktop_control_type,
        desktop_control_launch_app,
        desktop_control_focus_window,
        desktop_control_hotkey,
        desktop_control_scroll,
        desktop_control_drag,
    ],
)


# 全サブエージェントのリスト
SUB_AGENTS = [
    web_agent,
    file_agent,
    system_agent,
    memory_agent,
    browser_agent,
    desktop_agent,
]
