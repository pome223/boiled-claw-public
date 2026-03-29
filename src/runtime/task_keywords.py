"""Shared task keyword sets used across routing and control-loop normalization."""

from __future__ import annotations

CURRENT_BROWSER_KEYWORDS: frozenset[str] = frozenset(
    {
        "このブラウザ",
        "このタブ",
        "このページ",
        "このウィンドウ",
        "私が開いているブラウザ",
        "今開いているブラウザ",
        "開いているブラウザ",
        "今のブラウザ",
        "いまのブラウザ",
        "現在のブラウザ",
        "既存のブラウザ",
        "私のブラウザ",
        "current browser",
        "existing browser",
        "今開いているタブ",
        "開いているタブ",
        "今のタブ",
        "現在のタブ",
        "既存のタブ",
        "今開いているスプレッドシート",
        "開いているスプレッドシート",
        "今のスプレッドシート",
        "現在のスプレッドシート",
        "既存のスプレッドシート",
    }
)

SPREADSHEET_KEYWORDS: frozenset[str] = frozenset(
    {
        "spreadsheet",
        "spread sheet",
        "sheet",
        "google sheet",
        "google sheets",
        "googleスプレッドシート",
        "スプレッド",
        "すぷれっど",
        "スプシ",
        "スプレッドシート",
        "シート",
        "表計算",
        # Keep a few observed typo/OCR variants so current-browser heuristics remain
        # tolerant of slightly garbled user text.
        "スプレッドsーと",
        "スプレッドシーート",
    }
)

COMPUTER_USE_KEYWORDS: frozenset[str] = frozenset(
    {
        "computer use",
        "computer using",
        "computer operator",
        "gui automation",
        "gui operator",
        "visible browser",
        "visible ui",
        "画面を見て",
        "画面を見ながら",
        "画面を確認しながら",
        "見えているブラウザ",
        "見えてるブラウザ",
        "見えている画面",
        "guiを見て",
        "uiを見て",
        "目で見て操作",
    }
)
