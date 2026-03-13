"""
macOS-oriented desktop client tests.
"""

from __future__ import annotations

import sys

import pytest

from src.desktop import (
    DesktopAxFindRequest,
    DesktopAxSnapshotRequest,
    DesktopClickRequest,
    DesktopClearStopRequest,
    DesktopDragRequest,
    DesktopEmergencyStopRequest,
    DesktopElementSelector,
    FakeDesktopClient,
    PyObjCDesktopClient,
    build_default_desktop_client,
    DesktopFocusWindowRequest,
    DesktopFrontmostAppRequest,
    DesktopHotkeyRequest,
    DesktopLaunchAppRequest,
    DesktopRuntimeStatusRequest,
    DesktopScrollRequest,
    DesktopScreenshotRequest,
    DesktopTypeRequest,
    DesktopWaitElementRequest,
    DesktopWaitWindowRequest,
    DesktopWindowsRequest,
)


class _FakeFrontmostApplication:
    def __init__(self, name: str = "Safari", pid: int = 123):
        self._name = name
        self._pid = pid

    def localizedName(self):
        return self._name

    def processIdentifier(self):
        return self._pid

    def activateWithOptions_(self, _options):
        _FakeWorkspace.last_activated_pid = self._pid


class _FakeWorkspace:
    last_activated_pid = None

    def frontmostApplication(self):
        return _FakeFrontmostApplication()

    def runningApplications(self):
        return [
            _FakeFrontmostApplication(),
            _FakeFrontmostApplication("Notes", 456),
        ]


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
    kCGEventLeftMouseDragged = 16
    kCGMouseButtonLeft = 100
    kCGMouseButtonRight = 101
    kCGMouseButtonCenter = 102
    kCGHIDEventTap = 999
    kCGEventFlagMaskCommand = 1 << 0
    kCGEventFlagMaskShift = 1 << 1
    kCGEventFlagMaskAlternate = 1 << 2
    kCGEventFlagMaskControl = 1 << 3
    kCGEventFlagMaskSecondaryFn = 1 << 4
    kAXWindowsAttribute = "AXWindows"
    kAXChildrenAttribute = "AXChildren"
    kAXRoleAttribute = "AXRole"
    kAXTitleAttribute = "AXTitle"
    kAXDescriptionAttribute = "AXDescription"
    kAXValueAttribute = "AXValue"
    kAXIdentifierAttribute = "AXIdentifier"
    kAXPositionAttribute = "AXPosition"
    kAXSizeAttribute = "AXSize"
    kAXPressAction = "AXPress"
    kAXRaiseAction = "AXRaise"

    posted = []
    actions = []
    set_values = []

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
            "AXPosition": {"x": 100, "y": 200},
            "AXSize": {"x": 80, "y": 30},
            "AXChildren": [],
        }
        text_field = {
            "AXRole": "AXTextField",
            "AXTitle": "Search",
            "AXDescription": "",
            "AXValue": "",
            "AXIdentifier": "search-field",
            "AXPosition": {"x": 120, "y": 260},
            "AXSize": {"x": 220, "y": 28},
            "AXChildren": [],
        }
        window = {
            "AXRole": "AXWindow",
            "AXTitle": "Example",
            "AXDescription": "",
            "AXValue": None,
            "AXIdentifier": "window-7",
            "AXPosition": {"x": 10, "y": 20},
            "AXSize": {"x": 800, "y": 600},
            "AXChildren": [button, text_field],
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
    def AXUIElementPerformAction(cls, element, action):
        cls.actions.append((action, element.get("AXIdentifier")))
        return 0

    @classmethod
    def AXUIElementSetAttributeValue(cls, element, attribute, value):
        cls.set_values.append((element.get("AXIdentifier"), attribute, value))
        element[attribute] = value
        return 0

    @classmethod
    def CGEventCreateMouseEvent(cls, _source, event_type, point, button):
        return {"kind": "mouse", "event_type": event_type, "point": point, "button": button}

    @classmethod
    def CGEventCreateKeyboardEvent(cls, _source, keycode, is_down):
        return {"kind": "keyboard", "keycode": keycode, "is_down": is_down}

    @classmethod
    def CGEventCreateScrollWheelEvent(cls, _source, units, wheels, delta_y, delta_x):
        return {
            "kind": "scroll",
            "units": units,
            "wheels": wheels,
            "delta_y": delta_y,
            "delta_x": delta_x,
        }

    @classmethod
    def CGEventKeyboardSetUnicodeString(cls, event, length, text):
        event["length"] = length
        event["text"] = text

    @classmethod
    def CGEventSetFlags(cls, event, flags):
        event["flags"] = flags

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
async def test_pyobjc_client_ax_find_returns_target_descriptor():
    client = PyObjCDesktopClient(
        appkit_module=_FakeAppKit(),
        quartz_module=_FakeQuartz(),
    )

    result = await client.ax_find(
        DesktopAxFindRequest(
            request_id="req-find",
            session_id="sess",
            user_id="user",
            agent_name="pytest",
            target=DesktopElementSelector(
                app_name="Safari",
                window_id="7",
                role="AXButton",
                identifier="open-button",
            ),
        )
    )

    assert result.ok is True
    assert result.matched is True
    assert result.target is not None
    assert result.target.identifier == "open-button"


@pytest.mark.asyncio
async def test_pyobjc_client_wait_window_matches_existing_window():
    client = PyObjCDesktopClient(quartz_module=_FakeQuartz())

    result = await client.wait_window(
        DesktopWaitWindowRequest(
            request_id="req-wait-window",
            session_id="sess",
            user_id="user",
            agent_name="pytest",
            app_name="Safari",
            timeout_seconds=0.01,
            poll_interval_seconds=0.001,
        )
    )

    assert result.ok is True
    assert result.matched is True
    assert result.window is not None
    assert result.window.window_id == "7"


@pytest.mark.asyncio
async def test_pyobjc_client_wait_element_matches_existing_element():
    client = PyObjCDesktopClient(
        appkit_module=_FakeAppKit(),
        quartz_module=_FakeQuartz(),
    )

    result = await client.wait_element(
        DesktopWaitElementRequest(
            request_id="req-wait-element",
            session_id="sess",
            user_id="user",
            agent_name="pytest",
            target=DesktopElementSelector(
                app_name="Safari",
                window_id="7",
                identifier="open-button",
            ),
            timeout_seconds=0.01,
            poll_interval_seconds=0.001,
        )
    )

    assert result.ok is True
    assert result.matched is True
    assert result.target is not None
    assert result.target.identifier == "open-button"


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


@pytest.mark.asyncio
async def test_pyobjc_client_click_selector_uses_ax_press():
    _FakeQuartz.actions = []
    client = PyObjCDesktopClient(
        appkit_module=_FakeAppKit(),
        quartz_module=_FakeQuartz(),
    )

    result = await client.click(
        DesktopClickRequest(
            request_id="req-click-selector",
            session_id="sess",
            user_id="user",
            agent_name="pytest",
            target=DesktopElementSelector(
                app_name="Safari",
                window_id="7",
                role="AXButton",
                identifier="open-button",
            ),
        )
    )

    assert result.ok is True
    assert result.target is not None
    assert result.target.identifier == "open-button"
    assert _FakeQuartz.actions[-1] == (_FakeQuartz.kAXPressAction, "open-button")


@pytest.mark.asyncio
async def test_pyobjc_client_type_selector_sets_ax_value():
    _FakeQuartz.set_values = []
    client = PyObjCDesktopClient(
        appkit_module=_FakeAppKit(),
        quartz_module=_FakeQuartz(),
    )

    result = await client.type_text(
        DesktopTypeRequest(
            request_id="req-type-selector",
            session_id="sess",
            user_id="user",
            agent_name="pytest",
            text="hello",
            target=DesktopElementSelector(
                app_name="Safari",
                window_id="7",
                role="AXTextField",
                identifier="search-field",
            ),
        )
    )

    assert result.ok is True
    assert result.target is not None
    assert result.target.identifier == "search-field"
    assert _FakeQuartz.set_values[-1] == ("search-field", _FakeQuartz.kAXValueAttribute, "hello")


@pytest.mark.asyncio
async def test_pyobjc_client_launch_app_uses_open_runner_and_activates(monkeypatch):
    captured: list[list[str]] = []
    _FakeWorkspace.last_activated_pid = None

    def _runner(args: list[str]) -> None:
        captured.append(args)

    monkeypatch.setattr("src.desktop.pyobjc_client.shutil.which", lambda name: "/usr/bin/open" if name == "open" else None)
    client = PyObjCDesktopClient(
        appkit_module=_FakeAppKit(),
        open_runner=_runner,
    )

    result = await client.launch_app(
        DesktopLaunchAppRequest(
            request_id="req-launch",
            session_id="sess",
            user_id="user",
            agent_name="pytest",
            app_name="Notes",
        )
    )

    assert result.ok is True
    assert captured == [["open", "-a", "Notes"]]
    assert _FakeWorkspace.last_activated_pid == 456


@pytest.mark.asyncio
async def test_pyobjc_client_focus_window_raises_and_activates():
    _FakeQuartz.actions = []
    _FakeWorkspace.last_activated_pid = None
    client = PyObjCDesktopClient(
        appkit_module=_FakeAppKit(),
        quartz_module=_FakeQuartz(),
    )

    result = await client.focus_window(
        DesktopFocusWindowRequest(
            request_id="req-focus",
            session_id="sess",
            user_id="user",
            agent_name="pytest",
            app_name="Safari",
            window_id="7",
        )
    )

    assert result.ok is True
    assert result.target is not None
    assert result.target.window_id == "7"
    assert (_FakeQuartz.kAXRaiseAction, "window-7") in _FakeQuartz.actions
    assert _FakeWorkspace.last_activated_pid == 123


@pytest.mark.asyncio
async def test_pyobjc_client_hotkey_posts_keyboard_events_with_flags():
    _FakeQuartz.posted = []
    client = PyObjCDesktopClient(quartz_module=_FakeQuartz())

    result = await client.hotkey(
        DesktopHotkeyRequest(
            request_id="req-hotkey",
            session_id="sess",
            user_id="user",
            agent_name="pytest",
            keys=["command", "shift", "p"],
        )
    )

    assert result.ok is True
    assert len(_FakeQuartz.posted) == 2
    assert _FakeQuartz.posted[0][1]["flags"] == (
        _FakeQuartz.kCGEventFlagMaskCommand | _FakeQuartz.kCGEventFlagMaskShift
    )


@pytest.mark.asyncio
async def test_pyobjc_client_scroll_posts_scroll_event():
    _FakeQuartz.posted = []
    client = PyObjCDesktopClient(quartz_module=_FakeQuartz())

    result = await client.scroll(
        DesktopScrollRequest(
            request_id="req-scroll",
            session_id="sess",
            user_id="user",
            agent_name="pytest",
            delta_x=2,
            delta_y=-4,
        )
    )

    assert result.ok is True
    assert len(_FakeQuartz.posted) == 1
    assert _FakeQuartz.posted[0][1]["kind"] == "scroll"
    assert _FakeQuartz.posted[0][1]["delta_x"] == 2
    assert _FakeQuartz.posted[0][1]["delta_y"] == -4


@pytest.mark.asyncio
async def test_pyobjc_client_emergency_stop_blocks_control_until_cleared():
    client = PyObjCDesktopClient(quartz_module=_FakeQuartz())

    stopped = await client.emergency_stop(
        DesktopEmergencyStopRequest(
            request_id="req-stop",
            session_id="sess",
            user_id="user",
            agent_name="pytest",
            reason="abort run",
        )
    )
    status = await client.runtime_status(
        DesktopRuntimeStatusRequest(
            request_id="req-status",
            session_id="sess",
            user_id="user",
            agent_name="pytest",
        )
    )
    blocked = await client.click(
        DesktopClickRequest(
            request_id="req-click-stop",
            session_id="sess",
            user_id="user",
            agent_name="pytest",
            x=10,
            y=20,
        )
    )
    cleared = await client.clear_stop(
        DesktopClearStopRequest(
            request_id="req-clear",
            session_id="sess",
            user_id="user",
            agent_name="pytest",
        )
    )

    assert stopped.stopped is True
    assert status.stopped is True
    assert blocked.ok is False
    assert "stopped" in (blocked.error or "").lower()
    assert cleared.stopped is False


@pytest.mark.asyncio
async def test_pyobjc_client_drag_posts_mouse_sequence():
    _FakeQuartz.posted = []
    client = PyObjCDesktopClient(quartz_module=_FakeQuartz())

    result = await client.drag(
        DesktopDragRequest(
            request_id="req-drag",
            session_id="sess",
            user_id="user",
            agent_name="pytest",
            start_x=10,
            start_y=20,
            end_x=30,
            end_y=40,
        )
    )

    assert result.ok is True
    assert len(_FakeQuartz.posted) == 3
    assert _FakeQuartz.posted[1][1]["event_type"] == _FakeQuartz.kCGEventLeftMouseDragged


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
