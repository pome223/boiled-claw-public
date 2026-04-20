from src.browser.current_tab_bridge import (
    _normalize_current_tab_bridge_host,
    _origin_is_allowed,
)
from src.security.network import enforce_loopback_bind, is_loopback_host


def test_is_loopback_host_accepts_loopback_values():
    assert is_loopback_host("127.0.0.1") is True
    assert is_loopback_host("localhost") is True
    assert is_loopback_host("::1") is True


def test_is_loopback_host_rejects_remote_values():
    assert is_loopback_host("0.0.0.0") is False
    assert is_loopback_host("192.168.1.10") is False


def test_enforce_loopback_bind_rejects_remote_by_default():
    try:
        enforce_loopback_bind("0.0.0.0", service_name="Current Tab relay")
    except ValueError as exc:
        assert "loopback bind addresses" in str(exc)
    else:
        raise AssertionError("Expected ValueError for remote bind")


def test_enforce_loopback_bind_allows_remote_when_overridden():
    enforce_loopback_bind(
        "0.0.0.0",
        service_name="Current Tab relay",
        allow_remote_bind=True,
    )


def test_current_tab_origin_only_allows_chrome_extensions():
    assert _origin_is_allowed("chrome-extension://abcdefghijklmnop") is True
    assert _origin_is_allowed("http://localhost:18789") is False
    assert _origin_is_allowed("https://example.com") is False


def test_normalize_current_tab_bridge_host_maps_host_docker_internal_to_loopback():
    assert _normalize_current_tab_bridge_host("host.docker.internal") == "127.0.0.1"
    assert _normalize_current_tab_bridge_host("127.0.0.1") == "127.0.0.1"
