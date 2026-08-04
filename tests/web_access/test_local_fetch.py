from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.web import local


class Response:
    def __init__(self, status=200, headers=None, body=b"ok") -> None:
        self.status = status
        self.headers = headers or {"Content-Type": "text/plain"}
        self.body = body
        self.released = False

    def stream(self, _size, decode_content=True):
        assert decode_content is True
        yield self.body

    def release_conn(self):
        self.released = True


def test_resolve_rejects_mixed_public_private_dns(monkeypatch):
    monkeypatch.setattr(local.socket, "getaddrinfo", lambda *_args, **_kwargs: [
        (2, 1, 6, "", ("93.184.216.34", 443)),
        (2, 1, 6, "", ("127.0.0.1", 443)),
    ])
    with pytest.raises(local.LocalWebError, match="内网"):
        local.resolve_public_ips("example.test", 443)


def test_https_connection_is_ip_pinned_with_original_sni(monkeypatch):
    captured = {}
    response = Response()

    class Pool:
        def __init__(self, host, **kwargs):
            captured["host"] = host
            captured["kwargs"] = kwargs

        def request(self, method, target, **kwargs):
            captured["method"] = method
            captured["target"] = target
            captured["request"] = kwargs
            return response

        def close(self):
            pass

    monkeypatch.setattr(local, "resolve_public_ips", lambda _host, _port: ("93.184.216.34",))
    monkeypatch.setattr(local.urllib3, "HTTPSConnectionPool", Pool)
    result = local.local_fetch("https://example.test/docs?q=1")
    assert result.content == "ok"
    assert captured["host"] == "93.184.216.34"
    assert captured["kwargs"]["server_hostname"] == "example.test"
    assert captured["kwargs"]["assert_hostname"] == "example.test"
    assert captured["request"]["headers"]["Host"] == "example.test"
    assert captured["request"]["redirect"] is False
    assert captured["target"] == "/docs?q=1"
    assert response.released


@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "https://user:pass@example.test/",
    "https://example.test:8443/",
])
def test_url_restrictions(url):
    with pytest.raises(local.LocalWebError):
        local.local_fetch(url)


def test_redirect_rejects_cross_host_and_https_downgrade(monkeypatch):
    pool_stub = SimpleNamespace(close=lambda: None)
    responses = iter([
        Response(302, {"Location": "https://other.test/"}),
        Response(302, {"Location": "http://example.test/next"}),
    ])
    monkeypatch.setattr(local, "_open_pinned", lambda _url: (pool_stub, next(responses)))
    with pytest.raises(local.LocalWebError, match="跨主机"):
        local.local_fetch("https://example.test/")
    with pytest.raises(local.LocalWebError, match="降级"):
        local.local_fetch("https://example.test/")


def test_decompressed_size_limit(monkeypatch):
    pool_stub = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(
        local,
        "_open_pinned",
        lambda _url: (pool_stub, Response(body=b"x" * (local.MAX_FETCH_BYTES + 1))),
    )
    with pytest.raises(local.LocalWebError, match="解压后"):
        local.local_fetch("https://example.test/")
