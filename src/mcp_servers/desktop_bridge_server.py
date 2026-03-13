"""
Desktop Bridge MCP server skeleton.

GUI / Accessibility capabilities live here, separate from Host Bridge.

起動方法:
  python -m src.mcp_servers.desktop_bridge_server
  python -m src.mcp_servers.desktop_bridge_server --sse --host 127.0.0.1 --port 8767
"""

from __future__ import annotations

import argparse
from typing import Optional

from src.bridges.desktop_bridge_schema import (
    BridgePingResult,
    CapabilityDescriptor,
    CapabilityListResult,
    DesktopAxSnapshotRequest,
    DesktopAxSnapshotResult,
    DesktopClickRequest,
    DesktopControlResult,
    DesktopDragRequest,
    DesktopFrontmostAppRequest,
    DesktopFrontmostAppResult,
    DesktopHotkeyRequest,
    DesktopScreenshotRequest,
    DesktopScreenshotResult,
    DesktopTypeRequest,
    DesktopWindowsRequest,
    DesktopWindowsResult,
)

_NOT_IMPLEMENTED = (
    "Desktop Bridge skeleton only. GUI automation is not implemented yet."
)


def _capabilities() -> CapabilityListResult:
    return CapabilityListResult(
        capabilities=[
            CapabilityDescriptor(
                name="desktop.view.screenshot",
                risk="medium",
                requires_approval=True,
                description="Capture a desktop screenshot from the host OS.",
                implemented=False,
            ),
            CapabilityDescriptor(
                name="desktop.view.windows",
                risk="low",
                requires_approval=True,
                description="List visible windows on the host OS.",
                implemented=False,
            ),
            CapabilityDescriptor(
                name="desktop.view.frontmost_app",
                risk="low",
                requires_approval=True,
                description="Inspect the frontmost app on the host OS.",
                implemented=False,
            ),
            CapabilityDescriptor(
                name="desktop.ax.snapshot",
                risk="medium",
                requires_approval=True,
                description="Capture an accessibility tree snapshot from the host OS.",
                implemented=False,
            ),
            CapabilityDescriptor(
                name="desktop.control.click",
                risk="high",
                requires_approval=True,
                description="Click on the host desktop.",
                implemented=False,
            ),
            CapabilityDescriptor(
                name="desktop.control.type",
                risk="high",
                requires_approval=True,
                description="Type text into the host desktop.",
                implemented=False,
            ),
            CapabilityDescriptor(
                name="desktop.control.hotkey",
                risk="high",
                requires_approval=True,
                description="Send a hotkey to the host desktop.",
                implemented=False,
            ),
            CapabilityDescriptor(
                name="desktop.control.drag",
                risk="high",
                requires_approval=True,
                description="Drag the pointer on the host desktop.",
                implemented=False,
            ),
        ]
    )


def _not_implemented_control() -> DesktopControlResult:
    return DesktopControlResult(ok=False, error=_NOT_IMPLEMENTED)


def create_server(host: str = "127.0.0.1", port: int = 8767):
    from mcp.server.fastmcp import FastMCP
    from mcp.server.fastmcp.server import TransportSecuritySettings

    mcp = FastMCP("desktop-bridge")
    mcp.settings.host = host
    mcp.settings.port = port
    mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
    )

    transport_hint = "sse" if host != "stdio" else "stdio"

    @mcp.tool(name="ping", description="Desktop Bridge health probe.")
    def ping() -> dict:
        return BridgePingResult(
            service="desktop-bridge",
            version="v1-skeleton",
            transport=transport_hint,
        ).model_dump()

    @mcp.tool(name="capabilities.list", description="List Desktop Bridge capabilities.")
    def list_capabilities() -> dict:
        return _capabilities().model_dump()

    @mcp.tool(
        name="desktop.view.screenshot",
        description="Capture a screenshot from the host desktop.",
    )
    def desktop_view_screenshot(
        request_id: str,
        session_id: str,
        user_id: str,
        agent_name: str,
        path: Optional[str] = None,
        approval_token: Optional[str] = None,
    ) -> dict:
        _ = DesktopScreenshotRequest(
            request_id=request_id,
            session_id=session_id,
            user_id=user_id,
            agent_name=agent_name,
            approval_token=approval_token,
            path=path,
        )
        return DesktopScreenshotResult(ok=False, path=path, error=_NOT_IMPLEMENTED).model_dump()

    @mcp.tool(name="desktop.view.windows", description="List windows on the host desktop.")
    def desktop_view_windows(
        request_id: str,
        session_id: str,
        user_id: str,
        agent_name: str,
        include_minimized: bool = False,
        approval_token: Optional[str] = None,
    ) -> dict:
        _ = DesktopWindowsRequest(
            request_id=request_id,
            session_id=session_id,
            user_id=user_id,
            agent_name=agent_name,
            approval_token=approval_token,
            include_minimized=include_minimized,
        )
        return DesktopWindowsResult(ok=False, error=_NOT_IMPLEMENTED).model_dump()

    @mcp.tool(
        name="desktop.view.frontmost_app",
        description="Inspect the frontmost app on the host desktop.",
    )
    def desktop_view_frontmost_app(
        request_id: str,
        session_id: str,
        user_id: str,
        agent_name: str,
        approval_token: Optional[str] = None,
    ) -> dict:
        _ = DesktopFrontmostAppRequest(
            request_id=request_id,
            session_id=session_id,
            user_id=user_id,
            agent_name=agent_name,
            approval_token=approval_token,
        )
        return DesktopFrontmostAppResult(ok=False, error=_NOT_IMPLEMENTED).model_dump()

    @mcp.tool(
        name="desktop.ax.snapshot",
        description="Capture an accessibility tree from the host desktop.",
    )
    def desktop_ax_snapshot(
        request_id: str,
        session_id: str,
        user_id: str,
        agent_name: str,
        app_name: Optional[str] = None,
        window_id: Optional[str] = None,
        approval_token: Optional[str] = None,
    ) -> dict:
        _ = DesktopAxSnapshotRequest(
            request_id=request_id,
            session_id=session_id,
            user_id=user_id,
            agent_name=agent_name,
            approval_token=approval_token,
            app_name=app_name,
            window_id=window_id,
        )
        return DesktopAxSnapshotResult(ok=False, error=_NOT_IMPLEMENTED).model_dump()

    @mcp.tool(name="desktop.control.click", description="Click on the host desktop.")
    def desktop_control_click(
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
        _ = DesktopClickRequest(
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
        return _not_implemented_control().model_dump()

    @mcp.tool(name="desktop.control.type", description="Type text into the host desktop.")
    def desktop_control_type(
        request_id: str,
        session_id: str,
        user_id: str,
        agent_name: str,
        text: str,
        approval_token: Optional[str] = None,
    ) -> dict:
        _ = DesktopTypeRequest(
            request_id=request_id,
            session_id=session_id,
            user_id=user_id,
            agent_name=agent_name,
            approval_token=approval_token,
            text=text,
        )
        return _not_implemented_control().model_dump()

    @mcp.tool(name="desktop.control.hotkey", description="Send a hotkey to the host desktop.")
    def desktop_control_hotkey(
        request_id: str,
        session_id: str,
        user_id: str,
        agent_name: str,
        keys: list[str],
        approval_token: Optional[str] = None,
    ) -> dict:
        _ = DesktopHotkeyRequest(
            request_id=request_id,
            session_id=session_id,
            user_id=user_id,
            agent_name=agent_name,
            approval_token=approval_token,
            keys=keys,
        )
        return _not_implemented_control().model_dump()

    @mcp.tool(name="desktop.control.drag", description="Drag on the host desktop.")
    def desktop_control_drag(
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
        _ = DesktopDragRequest(
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
        return _not_implemented_control().model_dump()

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
