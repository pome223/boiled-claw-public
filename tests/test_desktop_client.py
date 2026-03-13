"""
Desktop client contract tests.
"""

import pytest

from src.desktop import (
    DesktopAxFindRequest,
    FakeDesktopClient,
    DesktopClickRequest,
    DesktopElementSelector,
    DesktopFocusWindowRequest,
    DesktopFrontmostAppRequest,
    DesktopLaunchAppRequest,
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


@pytest.mark.asyncio
async def test_fake_desktop_client_tracks_selector_click_and_launch_focus():
    client = FakeDesktopClient(
        implemented={
            "desktop.ax.find",
            "desktop.control.click",
            "desktop.control.launch_app",
            "desktop.control.focus_window",
        },
        windows=[
            DesktopWindowDescriptor(
                window_id="w1",
                app_name="Safari",
                title="Example",
                bounds=DesktopWindowBounds(x=10, y=20, width=800, height=600),
            )
        ],
    )

    click = await client.click(
        DesktopClickRequest(
            request_id="req-click",
            session_id="sess",
            user_id="user",
            agent_name="pytest",
            target=DesktopElementSelector(
                app_name="Safari",
                window_id="w1",
                title="Open",
            ),
        )
    )
    found = await client.ax_find(
        DesktopAxFindRequest(
            request_id="req-find",
            session_id="sess",
            user_id="user",
            agent_name="pytest",
            target=DesktopElementSelector(
                app_name="Safari",
                window_id="w1",
                title="Open",
            ),
        )
    )
    launch = await client.launch_app(
        DesktopLaunchAppRequest(
            request_id="req-launch",
            session_id="sess",
            user_id="user",
            agent_name="pytest",
            app_name="Safari",
        )
    )
    focus = await client.focus_window(
        DesktopFocusWindowRequest(
            request_id="req-focus",
            session_id="sess",
            user_id="user",
            agent_name="pytest",
            window_id="w1",
        )
    )

    assert click.ok is True
    assert click.target is not None
    assert click.target.window_id == "w1"
    assert found.ok is True
    assert found.matched is True
    assert launch.ok is True
    assert launch.target is not None
    assert launch.target.app_name == "Safari"
    assert focus.ok is True
    assert focus.target is not None
    assert focus.target.title == "Example"
