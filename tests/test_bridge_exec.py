from src.bridges.desktop_exec import _error_text as desktop_error_text
from src.bridges.host_bridge_exec import _error_text as host_error_text


def test_host_bridge_error_text_flattens_exception_group():
    exc = ExceptionGroup(
        "outer",
        [
            RuntimeError("page has been closed"),
            RuntimeError("page has been closed"),
            ValueError("inner failure"),
        ],
    )

    assert host_error_text(exc) == "page has been closed; inner failure"


def test_desktop_error_text_flattens_nested_exception_group():
    exc = ExceptionGroup(
        "outer",
        [
            ExceptionGroup("nested", [RuntimeError("Desktop Bridge is not enabled")]),
        ],
    )

    assert desktop_error_text(exc) == "Desktop Bridge is not enabled"
