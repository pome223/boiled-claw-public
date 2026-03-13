"""
Desktop client contract tests.
"""

import pytest

from src.desktop import (
    FakeDesktopClient,
    DesktopClickRequest,
    DesktopFrontmostAppRequest,
    DesktopWindowBounds,
    DesktopWindowDescriptor,
    DesktopWindowsRequest,
)


@pytest.mark.asyncio
async def test_fake_desktop_client_reports_implemented_capabilities():
    client = FakeDesktopClient(
        implemented={"desktop.view.windows", "desktop.view.frontmost_app"}
    )

    capabilities = await client.capabilities()
    by_name = {cap.name: cap for cap in capabilities.capabilities}

    assert by_name["desktop.view.windows"].implemented is True
    assert by_name["desktop.view.frontmost_app"].implemented is True
    assert by_name["desktop.control.click"].implemented is False


@pytest.mark.asyncio
async def test_fake_desktop_client_returns_canned_view_state():
    client = FakeDesktopClient(
        implemented={"desktop.view.windows", "desktop.view.frontmost_app"},
        windows=[
            DesktopWindowDescriptor(
                window_id="w1",
                app_name="Safari",
                title="Example",
                bounds=DesktopWindowBounds(x=10, y=20, width=800, height=600),
            )
        ],
        frontmost_app_name="Safari",
        frontmost_pid=123,
    )

    windows = await client.windows(
        DesktopWindowsRequest(
            request_id="req-windows",
            session_id="sess",
            user_id="user",
            agent_name="pytest",
        )
    )
    frontmost = await client.frontmost_app(
        DesktopFrontmostAppRequest(
            request_id="req-frontmost",
            session_id="sess",
            user_id="user",
            agent_name="pytest",
        )
    )

    assert windows.ok is True
    assert windows.windows[0].app_name == "Safari"
    assert frontmost.ok is True
    assert frontmost.app_name == "Safari"
    assert frontmost.pid == 123


@pytest.mark.asyncio
async def test_fake_desktop_client_returns_not_implemented_for_missing_control():
    client = FakeDesktopClient()

    result = await client.click(
        DesktopClickRequest(
            request_id="req-click",
            session_id="sess",
            user_id="user",
            agent_name="pytest",
            x=10,
            y=20,
        )
    )

    assert result.ok is False
    assert "not implemented" in (result.error or "").lower()
