"""Desktop client factory helpers."""

from __future__ import annotations

import os
import platform

from src.desktop.client import DesktopClient
from src.desktop.fake_client import FakeDesktopClient
from src.desktop.pyobjc_client import PyObjCDesktopClient


def build_default_desktop_client() -> DesktopClient:
    mode = os.getenv("BOILED_CLAW_DESKTOP_CLIENT", "auto").strip().lower()
    if mode == "fake":
        return FakeDesktopClient()

    if platform.system() != "Darwin":
        return FakeDesktopClient()

    appkit_module = None
    quartz_module = None

    try:
        import AppKit  # type: ignore

        appkit_module = AppKit
    except Exception:
        if mode == "pyobjc":
            return FakeDesktopClient()

    try:
        import Quartz  # type: ignore

        quartz_module = Quartz
    except Exception:
        if mode == "pyobjc":
            return FakeDesktopClient()

    return PyObjCDesktopClient(
        appkit_module=appkit_module,
        quartz_module=quartz_module,
    )


__all__ = ["build_default_desktop_client"]
