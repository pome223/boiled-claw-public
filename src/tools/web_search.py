"""
Web検索ツール
Google Search API または DuckDuckGo を使って情報を検索する
"""

import httpx
from google.adk.tools import FunctionTool


async def web_search(query: str) -> dict:
    """
    Webを検索して結果を返す

    Args:
        query: 検索クエリ

    Returns:
        検索結果のリスト
    """
    # DuckDuckGo Instant Answer API (API keyなしで利用可能)
    url = "https://api.duckduckgo.com/"
    params = {
        "q": query,
        "format": "json",
        "no_html": 1,
        "skip_disambig": 1,
    }

    async with httpx.AsyncClient() as client:
        response = await client.get(url, params=params, timeout=10.0)
        data = response.json()

    results = []

    # Abstract (要約)
    if data.get("Abstract"):
        results.append({
            "title": data.get("Heading", ""),
            "snippet": data["Abstract"],
            "url": data.get("AbstractURL", ""),
        })

    # Related Topics
    for topic in data.get("RelatedTopics", [])[:5]:
        if isinstance(topic, dict) and topic.get("Text"):
            results.append({
                "title": topic.get("Text", "")[:50],
                "snippet": topic.get("Text", ""),
                "url": topic.get("FirstURL", ""),
            })

    if not results:
        return {"results": [], "message": f"No results found for: {query}"}

    return {"results": results, "query": query}
