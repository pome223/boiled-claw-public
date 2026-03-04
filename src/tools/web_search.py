"""
Web検索ツール
duckduckgo-search ライブラリを使って実際のWeb検索結果を返す
"""

import asyncio
from typing import Any, Dict

VALID_TIMELIMITS = {"", "d", "w", "m", "y"}


def _normalize_timelimit(timelimit: str) -> str:
    value = (timelimit or "").strip().lower()
    return value if value in VALID_TIMELIMITS else ""


async def web_search(
    query: str,
    max_results: int = 5,
    timelimit: str = "",
    region: str = "jp-jp",
) -> Dict[str, Any]:
    """
    Webを検索して結果を返す。

    ツール利用ガイド:
    - 最新/今週/最近など時系列が重要な質問では `timelimit` を使う
      - d: 24時間以内
      - w: 1週間以内
      - m: 1か月以内
      - y: 1年以内
    - ファクト確認は最低1回はこのツールを使ってから回答する

    Args:
        query: 検索クエリ
        max_results: 取得する最大件数（デフォルト5）
        timelimit: d/w/m/y の期間フィルタ（未指定可）
        region: 検索リージョン（例: jp-jp, us-en）

    Returns:
        検索結果のリスト
    """
    try:
        from ddgs import DDGS
    except ImportError:
        return {
            "results": [],
            "query": query,
            "message": "ddgs library not installed",
        }

    normalized_timelimit = _normalize_timelimit(timelimit)
    normalized_max_results = max(1, min(10, int(max_results)))
    normalized_region = (region or "jp-jp").strip() or "jp-jp"

    def _search() -> list:
        with DDGS() as ddgs:
            return list(
                ddgs.text(
                    query,
                    max_results=normalized_max_results,
                    region=normalized_region,
                    timelimit=normalized_timelimit or None,
                )
            )

    try:
        raw = await asyncio.get_event_loop().run_in_executor(None, _search)
    except Exception as exc:
        return {"results": [], "query": query, "message": f"Search failed: {exc}"}

    if not raw:
        return {
            "results": [],
            "query": query,
            "message": f"No results found for: {query}",
            "meta": {
                "max_results": normalized_max_results,
                "region": normalized_region,
                "timelimit": normalized_timelimit,
            },
        }

    results = [
        {
            "title": item.get("title", ""),
            "snippet": item.get("body", ""),
            "url": item.get("href", ""),
        }
        for item in raw
    ]

    return {
        "results": results,
        "query": query,
        "meta": {
            "max_results": normalized_max_results,
            "region": normalized_region,
            "timelimit": normalized_timelimit,
        },
    }
