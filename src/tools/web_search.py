"""
Web検索ツール
duckduckgo-search ライブラリを使って実際のWeb検索結果を返す
"""

import asyncio
from typing import Any, Dict


async def web_search(query: str, max_results: int = 5) -> Dict[str, Any]:
    """
    Webを検索して結果を返す。

    Args:
        query: 検索クエリ
        max_results: 取得する最大件数（デフォルト5）

    Returns:
        検索結果のリスト
    """
    try:
        from duckduckgo_search import DDGS
    except ImportError:
        return {
            "results": [],
            "query": query,
            "message": "duckduckgo-search library not installed",
        }

    def _search() -> list:
        with DDGS() as ddgs:
            return list(ddgs.text(query, max_results=max_results))

    try:
        raw = await asyncio.get_event_loop().run_in_executor(None, _search)
    except Exception as exc:
        return {"results": [], "query": query, "message": f"Search failed: {exc}"}

    if not raw:
        return {"results": [], "query": query, "message": f"No results found for: {query}"}

    results = [
        {
            "title": item.get("title", ""),
            "snippet": item.get("body", ""),
            "url": item.get("href", ""),
        }
        for item in raw
    ]

    return {"results": results, "query": query}
