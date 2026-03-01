"""
株価取得ツール
無料の Stooq エンドポイントから日次株価を取得する
"""

from __future__ import annotations

import csv
from io import StringIO
from typing import Optional

import httpx
from google.adk.tools import FunctionTool


_ALIASES = {
    "nvidia": "NVDA.US",
    "nvda": "NVDA.US",
    "tesla": "TSLA.US",
    "tsla": "TSLA.US",
    "apple": "AAPL.US",
    "aapl": "AAPL.US",
    "microsoft": "MSFT.US",
    "msft": "MSFT.US",
    "google": "GOOGL.US",
    "alphabet": "GOOGL.US",
    "amazon": "AMZN.US",
    "meta": "META.US",
}


def _normalize_symbol(raw: str) -> Optional[str]:
    token = (raw or "").strip().lower()
    if not token:
        return None

    if token in _ALIASES:
        return _ALIASES[token]

    if token.endswith(".us"):
        return token.upper()

    # 英字ティッカーを想定
    if token.isalnum() and 1 <= len(token) <= 6:
        return f"{token.upper()}.US"

    # 文中に "Nvidiaの株価" のようなケース
    for k, v in _ALIASES.items():
        if k in token:
            return v

    return None


async def stock_price(symbol_or_name: str) -> dict:
    """
    株価を取得する（日次OHLC）

    Args:
        symbol_or_name: ティッカー or 会社名 (例: NVDA, NVIDIA)

    Returns:
        価格情報
    """
    symbol = _normalize_symbol(symbol_or_name)
    if not symbol:
        return {
            "ok": False,
            "message": "銘柄を特定できませんでした。例: NVDA, AAPL, TSLA",
        }

    url = "https://stooq.com/q/l/"
    params = {"s": symbol.lower(), "i": "d"}

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(url, params=params)
    except httpx.TimeoutException:
        return {"ok": False, "symbol": symbol, "message": "株価APIがタイムアウトしました。"}
    except httpx.HTTPError as exc:
        return {"ok": False, "symbol": symbol, "message": f"株価API呼び出しに失敗しました: {exc}"}

    if resp.status_code >= 400:
        return {
            "ok": False,
            "symbol": symbol,
            "message": f"株価APIエラー: HTTP {resp.status_code}",
        }

    text = resp.text.strip()
    row = None

    # Case 1: ヘッダ付きCSV
    reader = csv.DictReader(StringIO(text))
    parsed = next(reader, None)
    if parsed and any(k in parsed for k in ("Symbol", "Date", "Close")):
        row = parsed
    else:
        # Case 2: ヘッダなし 1行CSV
        # 例: NVDA.US,20260227,220020,181.25,182.59,176.38,177.19,310416947,
        first_line = text.splitlines()[0] if text else ""
        parts = [p.strip() for p in first_line.split(",")]
        if len(parts) >= 8:
            row = {
                "Symbol": parts[0],
                "Date": parts[1],
                "Time": parts[2],
                "Open": parts[3],
                "High": parts[4],
                "Low": parts[5],
                "Close": parts[6],
                "Volume": parts[7],
            }

    if not row:
        return {"ok": False, "symbol": symbol, "message": "株価データが見つかりませんでした。"}

    close = row.get("Close")
    if not close or close.upper() == "N/D":
        return {"ok": False, "symbol": symbol, "message": "有効な終値データがありません。"}

    return {
        "ok": True,
        "source": "stooq",
        "symbol": row.get("Symbol", symbol),
        "date": row.get("Date"),
        "time": row.get("Time"),
        "open": row.get("Open"),
        "high": row.get("High"),
        "low": row.get("Low"),
        "close": close,
        "volume": row.get("Volume"),
    }


stock_price_tool = FunctionTool(stock_price)
