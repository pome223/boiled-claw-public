from src.tools.web_search import _normalize_timelimit


def test_normalize_timelimit_accepts_valid_values():
    assert _normalize_timelimit("d") == "d"
    assert _normalize_timelimit("W") == "w"
    assert _normalize_timelimit(" m ") == "m"
    assert _normalize_timelimit("y") == "y"
    assert _normalize_timelimit("") == ""


def test_normalize_timelimit_rejects_invalid_values():
    assert _normalize_timelimit("week") == ""
    assert _normalize_timelimit("1w") == ""
    assert _normalize_timelimit("invalid") == ""
