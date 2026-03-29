from src.bridges.common_errors import flatten_exception_text


def test_flatten_exception_text_deduplicates_exception_group_messages():
    exc = ExceptionGroup(
        "outer",
        [
            RuntimeError("page has been closed"),
            RuntimeError("page has been closed"),
            ValueError("inner failure"),
        ],
    )

    assert flatten_exception_text(exc) == "page has been closed; inner failure"


def test_flatten_exception_text_flattens_nested_exception_group():
    exc = ExceptionGroup(
        "outer",
        [
            ExceptionGroup("nested", [RuntimeError("Desktop Bridge is not enabled")]),
        ],
    )

    assert flatten_exception_text(exc) == "Desktop Bridge is not enabled"
