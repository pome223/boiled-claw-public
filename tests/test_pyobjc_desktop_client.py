"""
macOS-oriented desktop client tests.
"""

from __future__ import annotations

import sys

import pytest

from src.desktop import (
    FakeDesktopClient,
    PyObjCDesktopClient,
    build_default_desktop_client,
    DesktopFrontmostAppRequest,
    DesktopScreenshotRequest,
    DesktopWindowsRequest,
)


class _FakeFrontmostApplication:
    def localizedName(self):
        return "Safari"

    def processIdentifier(self):
        return 123


class _FakeWorkspace:
    def frontmostApplication(self):
        return _FakeFrontmostApplication()


class _FakeNSWorkspace:
    @staticmethod
    def sharedWorkspace():
        return _FakeWorkspace()


class _FakeAppKit:
    NSWorkspace = _FakeNSWorkspace


class _FakeQuartz:
    kCGWindowListOptionAll = 1
    kCGWindowListOptionOnScreenOnly = 2
    kCGNullWindowID = 0

    @staticmethod
    def CGWindowListCopyWindowInfo(option, null_window_id):
        assert null_window_id == 0
        assert option in {1, 2}
        return [
            {
                "kCGWindowNumber": 7,
                "kCGWindowOwnerName": "Safari",
                "kCGWindowName": "Example",
                "kCGWindowLayer": 0,
                "kCGWindowBounds": {"X": 10, "Y": 20, "Width": 800, "Height": 600},
            },
            {
                "kCGWindowNumber": 8,
                "kCGWindowOwnerName": "Window Server",
                "kCGWindowLayer": 1,
            },
        ]

    @staticmethod
    def CGMainDisplayID():
        return 99

    @staticmethod
    def CGDisplayPixelsWide(display_id):
        assert display_id == 99
        return 1440

    @staticmethod
    def CGDisplayPixelsHigh(display_id):
        assert display_id == 99
        return 900


@pytest.mark.asyncio
async def test_pyobjc_client_frontmost_app_uses_appkit():
    client = PyObjCDesktopClient(appkit_module=_FakeAppKit())

    result = await client.frontmost_app(
        DesktopFrontmostAppRequest(
            request_id="req-frontmost",
            session_id="sess",
            user_id="user",
            agent_name="pytest",
        )
    )

    assert result.ok is True
    assert result.app_name == "Safari"
    assert result.pid == 123


@pytest.mark.asyncio
async def test_pyobjc_client_windows_uses_quartz():
    client = PyObjCDesktopClient(quartz_module=_FakeQuartz())

    result = await client.windows(
        DesktopWindowsRequest(
            request_id="req-windows",
            session_id="sess",
            user_id="user",
            agent_name="pytest",
        )
    )

    assert result.ok is True
    assert len(result.windows) == 1
    assert result.windows[0].app_name == "Safari"
    assert result.windows[0].bounds.width == 800


@pytest.mark.asyncio
async def test_pyobjc_client_screenshot_uses_runner(monkeypatch):
    captured: dict[str, str] = {}

    def _runner(path: str) -> None:
        captured["path"] = path

    monkeypatch.setattr("src.desktop.pyobjc_client.shutil.which", lambda _: "/usr/sbin/screencapture")
    client = PyObjCDesktopClient(quartz_module=_FakeQuartz(), screenshot_runner=_runner)

    result = await client.screenshot(
        DesktopScreenshotRequest(
            request_id="req-shot",
            session_id="sess",
            user_id="user",
            agent_name="pytest",
            path="/tmp/fake-shot.png",
        )
    )

    assert result.ok is True
    assert captured["path"] == "/tmp/fake-shot.png"
    assert result.width == 1440
    assert result.height == 900


def test_build_default_desktop_client_returns_fake_off_macos(monkeypatch):
    monkeypatch.setenv("BOILED_CLAW_DESKTOP_CLIENT", "auto")
    monkeypatch.setattr("src.desktop.factory.platform.system", lambda: "Linux")

    client = build_default_desktop_client()

    assert isinstance(client, FakeDesktopClient)


def test_build_default_desktop_client_returns_pyobjc_when_modules_exist(monkeypatch):
    monkeypatch.setenv("BOILED_CLAW_DESKTOP_CLIENT", "auto")
    monkeypatch.setattr("src.desktop.factory.platform.system", lambda: "Darwin")
    monkeypatch.setitem(sys.modules, "AppKit", _FakeAppKit())
    monkeypatch.setitem(sys.modules, "Quartz", _FakeQuartz())

    client = build_default_desktop_client()

    assert isinstance(client, PyObjCDesktopClient)
