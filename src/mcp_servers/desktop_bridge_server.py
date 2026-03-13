"""
Desktop Bridge MCP server adapter.

GUI / Accessibility capabilities live here, separate from Host Bridge.
This server delegates to a DesktopClient implementation.

起動方法:
  python -m src.mcp_servers.desktop_bridge_server
  python -m src.mcp_servers.desktop_bridge_server --sse --host 127.0.0.1 --port 8767
"""

from __future__ import annotations

import argparse
from typing import Optional

from src.desktop import (
    BridgePingResult,
    DesktopAxSnapshotRequest,
    build_default_desktop_client,
    DesktopClient,
    DesktopClickRequest,
    DesktopDragRequest,
    DesktopFrontmostAppRequest,
    DesktopHotkeyRequest,
    DesktopScreenshotRequest,
    DesktopTypeRequest,
    DesktopWindowsRequest,
)


def create_server(
    host: str = "127.0.0.1",
    port: int = 8767,
    *,
    desktop_client: DesktopClient | None = None,
):
    from mcp.server.fastmcp import FastMCP
    from mcp.server.fastmcp.server import TransportSecuritySettings

    mcp = FastMCP("desktop-bridge")
    mcp.settings.host = host
    mcp.settings.port = port
    mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
    )

    transport_hint = "sse" if host != "stdio" else "stdio"
    client = desktop_client or build_default_desktop_client()

    @mcp.tool(name="ping", description="Desktop Bridge health probe.")
    def ping() -> dict:
        return BridgePingResult(
            service="desktop-bridge",
            version="v1-client-adapter",
            transport=transport_hint,
        ).model_dump()

    @mcp.tool(name="capabilities.list", description="List Desktop Bridge capabilities.")
    async def list_capabilities() -> dict:
        return (await client.capabilities()).model_dump()

    @mcp.tool(
        name="desktop.view.screenshot",
        description="Capture a screenshot from the host desktop.",
    )
    async def desktop_view_screenshot(
        request_id: str,
        session_id: str,
        user_id: str,
        agent_name: str,
        path: Optional[str] = None,
        approval_token: Optional[str] = None,
    ) -> dict:
        request = DesktopScreenshotRequest(
            request_id=request_id,
            session_id=session_id,
            user_id=user_id,
            agent_name=agent_name,
            approval_token=approval_token,
            path=path,
        )
        return (await client.screenshot(request)).model_dump()

    @mcp.tool(name="desktop.view.windows", description="List windows on the host desktop.")
    async def desktop_view_windows(
        request_id: str,
        session_id: str,
        user_id: str,
        agent_name: str,
        include_minimized: bool = False,
        approval_token: Optional[str] = None,
    ) -> dict:
        request = DesktopWindowsRequest(
            request_id=request_id,
            session_id=session_id,
            user_id=user_id,
            agent_name=agent_name,
            approval_token=approval_token,
            include_minimized=include_minimized,
        )
        return (await client.windows(request)).model_dump()

    @mcp.tool(
        name="desktop.view.frontmost_app",
        description="Inspect the frontmost app on the host desktop.",
    )
    async def desktop_view_frontmost_app(
        request_id: str,
        session_id: str,
        user_id: str,
        agent_name: str,
        approval_token: Optional[str] = None,
    ) -> dict:
        request = DesktopFrontmostAppRequest(
            request_id=request_id,
            session_id=session_id,
            user_id=user_id,
            agent_name=agent_name,
            approval_token=approval_token,
        )
        return (await client.frontmost_app(request)).model_dump()

    @mcp.tool(
        name="desktop.ax.snapshot",
        description="Capture an accessibility tree from the host desktop.",
    )
    async def desktop_ax_snapshot(
        request_id: str,
        session_id: str,
        user_id: str,
        agent_name: str,
        app_name: Optional[str] = None,
        window_id: Optional[str] = None,
        approval_token: Optional[str] = None,
    ) -> dict:
        request = DesktopAxSnapshotRequest(
            request_id=request_id,
            session_id=session_id,
            user_id=user_id,
            agent_name=agent_name,
            approval_token=approval_token,
            app_name=app_name,
            window_id=window_id,
        )
        return (await client.ax_snapshot(request)).model_dump()

    @mcp.tool(name="desktop.control.click", description="Click on the host desktop.")
    async def desktop_control_click(
        request_id: str,
        session_id: str,
        user_id: str,
        agent_name: str,
        x: int,
        y: int,
        button: str = "left",
        click_count: int = 1,
        approval_token: Optional[str] = None,
    ) -> dict:
        request = DesktopClickRequest(
            request_id=request_id,
            session_id=session_id,
            user_id=user_id,
            agent_name=agent_name,
            approval_token=approval_token,
            x=x,
            y=y,
            button=button,
            click_count=click_count,
        )
        return (await client.click(request)).model_dump()

    @mcp.tool(name="desktop.control.type", description="Type text into the host desktop.")
    async def desktop_control_type(
        request_id: str,
        session_id: str,
        user_id: str,
        agent_name: str,
        text: str,
        approval_token: Optional[str] = None,
    ) -> dict:
        request = DesktopTypeRequest(
            request_id=request_id,
            session_id=session_id,
            user_id=user_id,
            agent_name=agent_name,
            approval_token=approval_token,
            text=text,
        )
        return (await client.type_text(request)).model_dump()

    @mcp.tool(name="desktop.control.hotkey", description="Send a hotkey to the host desktop.")
    async def desktop_control_hotkey(
        request_id: str,
        session_id: str,
        user_id: str,
        agent_name: str,
        keys: list[str],
        approval_token: Optional[str] = None,
    ) -> dict:
        request = DesktopHotkeyRequest(
            request_id=request_id,
            session_id=session_id,
            user_id=user_id,
            agent_name=agent_name,
            approval_token=approval_token,
            keys=keys,
        )
        return (await client.hotkey(request)).model_dump()

    @mcp.tool(name="desktop.control.drag", description="Drag on the host desktop.")
    async def desktop_control_drag(
        request_id: str,
        session_id: str,
        user_id: str,
        agent_name: str,
        start_x: int,
        start_y: int,
        end_x: int,
        end_y: int,
        approval_token: Optional[str] = None,
    ) -> dict:
        request = DesktopDragRequest(
            request_id=request_id,
            session_id=session_id,
            user_id=user_id,
            agent_name=agent_name,
            approval_token=approval_token,
            start_x=start_x,
            start_y=start_y,
            end_x=end_x,
            end_y=end_y,
        )
        return (await client.drag(request)).model_dump()

    return mcp


def main():
    parser = argparse.ArgumentParser(description="Desktop Bridge MCP Server")
    parser.add_argument("--sse", action="store_true", help="Run in SSE mode")
    parser.add_argument("--port", type=int, default=8767, help="SSE port")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="SSE host")
    args = parser.parse_args()

    if args.sse:
        mcp = create_server(host=args.host, port=args.port)
        print(f"SSE mode: http://{args.host}:{args.port}/sse")
        mcp.run(transport="sse")
    else:
        mcp = create_server(host="stdio")
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
