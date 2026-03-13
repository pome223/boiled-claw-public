"""
macOS-oriented desktop client tests.
"""

from __future__ import annotations

import sys

import pytest

from src.desktop import (
    DesktopAxSnapshotRequest,
    DesktopClickRequest,
    FakeDesktopClient,
    PyObjCDesktopClient,
    build_default_desktop_client,
    DesktopFrontmostAppRequest,
    DesktopScreenshotRequest,
    DesktopTypeRequest,
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

    def runningApplications(self):
        return [_FakeFrontmostApplication()]


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
    kCGEventLeftMouseDown = 10
    kCGEventLeftMouseUp = 11
    kCGEventRightMouseDown = 12
    kCGEventRightMouseUp = 13
    kCGEventOtherMouseDown = 14
    kCGEventOtherMouseUp = 15
    kCGMouseButtonLeft = 100
    kCGMouseButtonRight = 101
    kCGMouseButtonCenter = 102
    kCGHIDEventTap = 999
    kAXWindowsAttribute = "AXWindows"
    kAXChildrenAttribute = "AXChildren"
    kAXRoleAttribute = "AXRole"
    kAXTitleAttribute = "AXTitle"
    kAXDescriptionAttribute = "AXDescription"
    kAXValueAttribute = "AXValue"
    kAXIdentifierAttribute = "AXIdentifier"

    posted = []

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

    @classmethod
    def AXUIElementCreateApplication(cls, pid):
        assert pid == 123
        button = {
            "AXRole": "AXButton",
            "AXTitle": "Open",
            "AXDescription": "",
            "AXValue": None,
            "AXIdentifier": "open-button",
            "AXChildren": [],
        }
        window = {
            "AXRole": "AXWindow",
            "AXTitle": "Example",
            "AXDescription": "",
            "AXValue": None,
            "AXIdentifier": "window-7",
            "AXChildren": [button],
        }
        return {
            "AXRole": "AXApplication",
            "AXTitle": "Safari",
            "AXDescription": "",
            "AXValue": None,
            "AXIdentifier": "app-safari",
            "AXChildren": [window],
            "AXWindows": [window],
        }

    @staticmethod
    def AXUIElementCopyAttributeValue(element, attribute, _unused=None):
        return (0, element.get(attribute))

    @classmethod
    def CGEventCreateMouseEvent(cls, _source, event_type, point, button):
        return {"kind": "mouse", "event_type": event_type, "point": point, "button": button}

    @classmethod
    def CGEventCreateKeyboardEvent(cls, _source, keycode, is_down):
        return {"kind": "keyboard", "keycode": keycode, "is_down": is_down}

    @classmethod
    def CGEventKeyboardSetUnicodeString(cls, event, length, text):
        event["length"] = length
        event["text"] = text

    @classmethod
    def CGEventPost(cls, tap, event):
        cls.posted.append((tap, event.copy()))


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


@pytest.mark.asyncio
async def test_pyobjc_client_ax_snapshot_uses_ax_tree():
    client = PyObjCDesktopClient(
        appkit_module=_FakeAppKit(),
        quartz_module=_FakeQuartz(),
    )

    result = await client.ax_snapshot(
        DesktopAxSnapshotRequest(
            request_id="req-ax",
            session_id="sess",
            user_id="user",
            agent_name="pytest",
            app_name="Safari",
            window_id="7",
        )
    )

    assert result.ok is True
    assert result.tree["app_name"] == "Safari"
    assert result.tree["root"]["role"] == "AXWindow"
    assert result.tree["root"]["children"][0]["role"] == "AXButton"


@pytest.mark.asyncio
async def test_pyobjc_client_click_posts_mouse_events():
    _FakeQuartz.posted = []
    client = PyObjCDesktopClient(quartz_module=_FakeQuartz())

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

    assert result.ok is True
    assert len(_FakeQuartz.posted) == 2
    assert _FakeQuartz.posted[0][1]["event_type"] == _FakeQuartz.kCGEventLeftMouseDown
    assert _FakeQuartz.posted[1][1]["event_type"] == _FakeQuartz.kCGEventLeftMouseUp


@pytest.mark.asyncio
async def test_pyobjc_client_type_posts_keyboard_events():
    _FakeQuartz.posted = []
    client = PyObjCDesktopClient(quartz_module=_FakeQuartz())

    result = await client.type_text(
        DesktopTypeRequest(
            request_id="req-type",
            session_id="sess",
            user_id="user",
            agent_name="pytest",
            text="ab",
        )
    )

    assert result.ok is True
    assert len(_FakeQuartz.posted) == 4
    assert _FakeQuartz.posted[0][1]["text"] == "a"
    assert _FakeQuartz.posted[2][1]["text"] == "b"


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
