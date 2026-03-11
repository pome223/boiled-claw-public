"""
ブラウザ自動化ツール - Playwright
OpenClaw のブラウザ自動化機能を参考
"""

import asyncio
import ipaddress
from typing import Optional, Dict, Any, Tuple
from pathlib import Path
from urllib.parse import urlparse

from google.adk.agents.context import Context as ToolContext

from src.security.audit import AuditEventType, get_audit_logger
from src.tools.context import resolve_tool_context

# Playwright は遅延インポート (インストールされていない場合のエラー回避)
playwright = None
async_playwright = None

try:
    from playwright.async_api import async_playwright, Browser, Page
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False


class BrowserSession:
    """ブラウザセッション管理"""

    def __init__(self):
        self.playwright = None
        self.browser: Optional[Browser] = None
        self.page: Optional[Page] = None

    async def start(self, headless: bool = True):
        """ブラウザセッションを開始"""
        if not PLAYWRIGHT_AVAILABLE:
            raise RuntimeError(
                "Playwright is not installed. Run: pip install playwright && playwright install"
            )

        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=headless)
        self.page = await self.browser.new_page()

    async def close(self):
        """ブラウザセッションを閉じる"""
        if self.page:
            await self.page.close()
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()


_ALLOWED_SCHEMES = {"http", "https"}
_PRIVATE_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
]
_BLOCKED_HOSTS = {"localhost", "localhost.localdomain"}


def _audit_browser_event(
    *,
    action: str,
    resource: str,
    result: str,
    metadata: Dict[str, Any],
    tool_context: Optional[ToolContext],
) -> None:
    ctx = resolve_tool_context(tool_context) if tool_context is not None else {}
    get_audit_logger().log(
        event_type=AuditEventType.BROWSER_NAVIGATE,
        user_id=ctx.get("user_id") or None,
        session_id=ctx.get("session_id") or None,
        action=action,
        resource=resource,
        result=result,
        metadata=metadata,
    )


def _validate_url(url: str) -> Tuple[bool, Optional[str]]:
    """URL の安全性を検証する (SSRF 対策)"""
    try:
        parsed = urlparse(url)
    except Exception:
        return False, "Invalid URL"

    if parsed.scheme not in _ALLOWED_SCHEMES:
        return False, f"Scheme '{parsed.scheme}' is not allowed (only http/https)"

    hostname = parsed.hostname
    if not hostname:
        return False, "Missing hostname"

    if hostname.lower() in _BLOCKED_HOSTS:
        return False, f"Access to '{hostname}' is blocked"

    try:
        ip = ipaddress.ip_address(hostname)
        for network in _PRIVATE_NETWORKS:
            if ip in network:
                return False, f"Access to private/loopback address {ip} is blocked"
    except ValueError:
        pass  # ホスト名（IP でない）は IP チェックをスキップ

    return True, None


# グローバルセッション (再利用のため)
_browser_session: Optional[BrowserSession] = None


async def get_browser_session() -> BrowserSession:
    """ブラウザセッションを取得"""
    global _browser_session
    if _browser_session is None:
        _browser_session = BrowserSession()
        await _browser_session.start()
    return _browser_session


async def browser_navigate(
    url: str,
    wait_for: str = "load",
    timeout: int = 30000,
    tool_context: Optional[ToolContext] = None,
) -> Dict[str, Any]:
    """
    URLに移動してページを読み込む

    Args:
        url: 移動先URL
        wait_for: 待機イベント ('load', 'domcontentloaded', 'networkidle')
        timeout: タイムアウト (ミリ秒)

    Returns:
        ページ情報 (title, url, status)
    """
    if not PLAYWRIGHT_AVAILABLE:
        payload = {
            "error": "Playwright is not installed. Run: pip install playwright && playwright install"
        }
        _audit_browser_event(
            action="navigate",
            resource=url,
            result="error",
            metadata={"reason": "playwright_missing"},
            tool_context=tool_context,
        )
        return payload

    valid, reason = _validate_url(url)
    if not valid:
        payload = {"error": f"URL blocked: {reason}", "url": url, "success": False}
        _audit_browser_event(
            action="navigate",
            resource=url,
            result=f"blocked:{reason}",
            metadata={"wait_for": wait_for, "timeout": timeout},
            tool_context=tool_context,
        )
        return payload

    try:
        session = await get_browser_session()
        page = session.page

        response = await page.goto(url, wait_until=wait_for, timeout=timeout)

        payload = {
            "url": page.url,
            "title": await page.title(),
            "status": response.status if response else None,
            "success": True,
        }
        _audit_browser_event(
            action="navigate",
            resource=url,
            result="success",
            metadata={"status": payload["status"], "title": payload["title"]},
            tool_context=tool_context,
        )
        return payload

    except Exception as e:
        payload = {
            "error": str(e),
            "url": url,
            "success": False,
        }
        _audit_browser_event(
            action="navigate",
            resource=url,
            result=f"error:{e}",
            metadata={"wait_for": wait_for, "timeout": timeout},
            tool_context=tool_context,
        )
        return payload


async def browser_screenshot(
    path: Optional[str] = None,
    full_page: bool = False,
    tool_context: Optional[ToolContext] = None,
) -> Dict[str, Any]:
    """
    現在のページのスクリーンショットを撮る

    Args:
        path: 保存先パス (指定しない場合は自動生成)
        full_page: ページ全体をキャプチャ

    Returns:
        スクリーンショット情報
    """
    if not PLAYWRIGHT_AVAILABLE:
        payload = {
            "error": "Playwright is not installed. Run: pip install playwright && playwright install"
        }
        _audit_browser_event(
            action="screenshot",
            resource=path or "",
            result="error",
            metadata={"reason": "playwright_missing"},
            tool_context=tool_context,
        )
        return payload

    try:
        session = await get_browser_session()
        page = session.page

        if path is None:
            screenshots_dir = Path("data/screenshots")
            screenshots_dir.mkdir(parents=True, exist_ok=True)
            import time
            path = str(screenshots_dir / f"screenshot_{int(time.time())}.png")

        await page.screenshot(path=path, full_page=full_page)

        payload = {
            "path": path,
            "full_page": full_page,
            "success": True,
        }
        _audit_browser_event(
            action="screenshot",
            resource=path,
            result="success",
            metadata={"full_page": full_page},
            tool_context=tool_context,
        )
        return payload

    except Exception as e:
        payload = {
            "error": str(e),
            "success": False,
        }
        _audit_browser_event(
            action="screenshot",
            resource=path or "",
            result=f"error:{e}",
            metadata={"full_page": full_page},
            tool_context=tool_context,
        )
        return payload


async def browser_extract_text(
    selector: Optional[str] = None,
    tool_context: Optional[ToolContext] = None,
) -> Dict[str, Any]:
    """
    ページからテキストを抽出する

    Args:
        selector: CSSセレクタ (指定しない場合はbody全体)

    Returns:
        抽出されたテキスト
    """
    if not PLAYWRIGHT_AVAILABLE:
        payload = {
            "error": "Playwright is not installed. Run: pip install playwright && playwright install"
        }
        _audit_browser_event(
            action="extract_text",
            resource=selector or "body",
            result="error",
            metadata={"reason": "playwright_missing"},
            tool_context=tool_context,
        )
        return payload

    try:
        session = await get_browser_session()
        page = session.page

        if selector:
            element = await page.query_selector(selector)
            if element:
                text = await element.inner_text()
            else:
                payload = {
                    "error": f"Element not found: {selector}",
                    "success": False,
                }
                _audit_browser_event(
                    action="extract_text",
                    resource=selector,
                    result="not_found",
                    metadata={},
                    tool_context=tool_context,
                )
                return payload
        else:
            text = await page.inner_text("body")

        payload = {
            "text": text,
            "selector": selector or "body",
            "length": len(text),
            "success": True,
        }
        _audit_browser_event(
            action="extract_text",
            resource=payload["selector"],
            result="success",
            metadata={"length": payload["length"]},
            tool_context=tool_context,
        )
        return payload

    except Exception as e:
        payload = {
            "error": str(e),
            "success": False,
        }
        _audit_browser_event(
            action="extract_text",
            resource=selector or "body",
            result=f"error:{e}",
            metadata={},
            tool_context=tool_context,
        )
        return payload


async def browser_click(selector: str, timeout: int = 30000) -> Dict[str, Any]:
    """
    要素をクリックする

    Args:
        selector: CSSセレクタ
        timeout: タイムアウト (ミリ秒)

    Returns:
        クリック結果
    """
    if not PLAYWRIGHT_AVAILABLE:
        return {
            "error": "Playwright is not installed. Run: pip install playwright && playwright install"
        }

    try:
        session = await get_browser_session()
        page = session.page

        await page.click(selector, timeout=timeout)

        return {
            "selector": selector,
            "success": True,
        }

    except Exception as e:
        return {
            "error": str(e),
            "selector": selector,
            "success": False,
        }


async def browser_fill(selector: str, text: str, timeout: int = 30000) -> Dict[str, Any]:
    """
    フォーム入力フィールドに入力する

    Args:
        selector: CSSセレクタ
        text: 入力テキスト
        timeout: タイムアウト (ミリ秒)

    Returns:
        入力結果
    """
    if not PLAYWRIGHT_AVAILABLE:
        return {
            "error": "Playwright is not installed. Run: pip install playwright && playwright install"
        }

    try:
        session = await get_browser_session()
        page = session.page

        await page.fill(selector, text, timeout=timeout)

        return {
            "selector": selector,
            "text": text,
            "success": True,
        }

    except Exception as e:
        return {
            "error": str(e),
            "selector": selector,
            "success": False,
        }
