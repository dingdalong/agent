"""本地 Web 搜索与防 SSRF 网页抓取后端。"""

from __future__ import annotations

import ipaddress
import re
import socket
import ssl
import time
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit

import urllib3
from ddgs import DDGS

from src.web.types import WebFetchResponse, WebSearchResponse, WebSource


MAX_FETCH_BYTES = 1024 * 1024
MAX_REDIRECTS = 5
REQUEST_TIMEOUT_SECONDS = 15.0
USER_AGENT = "agent-web/1.0"
READABLE_CONTENT_TYPES = frozenset({
    "application/json",
    "application/ld+json",
    "application/xhtml+xml",
    "application/xml",
    "text/css",
    "text/csv",
    "text/html",
    "text/markdown",
    "text/plain",
    "text/xml",
})


class LocalWebError(RuntimeError):
    """本地 Web 后端返回的安全错误。"""


class _ReadableHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "svg", "canvas"}:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True
        elif tag in {"p", "div", "section", "article", "header", "footer", "br", "li"}:
            self.text_parts.append("\n")
        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self.text_parts.append("\n\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "svg", "canvas"} and self._skip_depth:
            self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in {"p", "div", "section", "article", "li", "h1", "h2", "h3", "h4", "h5", "h6"}:
            self.text_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self.title_parts.append(data)
        else:
            self.text_parts.append(data)


def _collapse_text(text: str) -> str:
    return "\n".join(
        normalized
        for line in text.splitlines()
        if (normalized := re.sub(r"[ \t\r\f\v]+", " ", line).strip())
    )


def extract_llm_text(html: str) -> tuple[str, str]:
    parser = _ReadableHTMLParser()
    parser.feed(html)
    return (
        _collapse_text(" ".join(parser.title_parts)),
        _collapse_text(unescape("".join(parser.text_parts))),
    )


def local_search(query: str, max_results: int = 5) -> WebSearchResponse:
    results = DDGS().text(
        query,
        region="wt-wt",
        safesearch="strict",
        timelimit="w",
        max_results=max_results,
    )
    sources = tuple(
        WebSource(
            url=str(item.get("href") or ""),
            title=str(item.get("title") or "").strip(),
            snippet=str(item.get("body") or "").strip()[:1000],
        )
        for item in results
        if isinstance(item, dict) and item.get("href")
    )
    return WebSearchResponse(summary="", sources=sources)


def _normalize_url(raw_url: str) -> str:
    try:
        parsed = urlsplit(raw_url.strip())
        port = parsed.port
    except ValueError as exc:
        raise LocalWebError("URL 格式无效") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise LocalWebError("仅支持带主机名的 http/https URL")
    if parsed.username is not None or parsed.password is not None:
        raise LocalWebError("URL 不得包含用户凭据")
    expected_port = 80 if parsed.scheme == "http" else 443
    if port not in {None, expected_port}:
        raise LocalWebError("仅允许 HTTP/HTTPS 标准端口")
    hostname = parsed.hostname.lower().rstrip(".")
    netloc = f"[{hostname}]" if ":" in hostname else hostname
    if port is not None:
        netloc += f":{port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path or "/", parsed.query, ""))


def resolve_public_ips(hostname: str, port: int) -> tuple[str, ...]:
    try:
        infos = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise LocalWebError("DNS 解析失败") from exc
    addresses = tuple(dict.fromkeys(info[4][0] for info in infos))
    if not addresses:
        raise LocalWebError("DNS 未返回可用地址")
    try:
        parsed = tuple(ipaddress.ip_address(address) for address in addresses)
    except ValueError as exc:
        raise LocalWebError("DNS 返回了无效地址") from exc
    if not all(address.is_global for address in parsed):
        raise LocalWebError("隐私保护：拒绝访问本机、内网、保留或云元数据地址")
    return addresses


def _host_header(hostname: str) -> str:
    return f"[{hostname}]" if ":" in hostname else hostname


def _open_pinned(
    url: str,
) -> tuple[urllib3.HTTPConnectionPool, urllib3.response.BaseHTTPResponse]:
    parsed = urlsplit(url)
    hostname = parsed.hostname or ""
    port = 80 if parsed.scheme == "http" else 443
    ip = resolve_public_ips(hostname, port)[0]
    headers = {
        "Accept": "text/html, text/plain, application/xhtml+xml, application/json;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Host": _host_header(hostname),
        "User-Agent": USER_AGENT,
    }
    timeout = urllib3.Timeout(connect=REQUEST_TIMEOUT_SECONDS, read=REQUEST_TIMEOUT_SECONDS)
    if parsed.scheme == "https":
        pool: urllib3.HTTPConnectionPool = urllib3.HTTPSConnectionPool(
            ip,
            port=port,
            timeout=timeout,
            retries=False,
            cert_reqs=ssl.CERT_REQUIRED,
            assert_hostname=hostname,
            server_hostname=hostname,
        )
    else:
        pool = urllib3.HTTPConnectionPool(ip, port=port, timeout=timeout, retries=False)
    target = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    response = pool.request(
        "GET",
        target,
        headers=headers,
        redirect=False,
        preload_content=False,
        decode_content=False,
    )
    return pool, response


def _check_response_headers(response: urllib3.response.BaseHTTPResponse) -> str:
    if "attachment" in response.headers.get("Content-Disposition", "").lower():
        raise LocalWebError("响应是附件下载，web_fetch 只读取网页或文本")
    content_type = response.headers.get("Content-Type", "")
    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type and not (
        media_type.startswith("text/")
        or media_type in READABLE_CONTENT_TYPES
        or media_type.endswith("+json")
        or media_type.endswith("+xml")
    ):
        raise LocalWebError(f"不支持的内容类型：{media_type}")
    raw_length = response.headers.get("Content-Length")
    if raw_length:
        try:
            if int(raw_length) > MAX_FETCH_BYTES:
                raise LocalWebError("网页内容超过 1 MiB 上限")
        except ValueError:
            pass
    return content_type


def _read_limited(response: urllib3.response.BaseHTTPResponse) -> bytes:
    body = bytearray()
    for chunk in response.stream(64 * 1024, decode_content=True):
        body.extend(chunk)
        if len(body) > MAX_FETCH_BYTES:
            raise LocalWebError("网页解压后内容超过 1 MiB 上限")
    return bytes(body)


def _decode_body(data: bytes, content_type: str) -> str:
    match = re.search(r"charset=([\w.-]+)", content_type, re.IGNORECASE)
    charset = match.group(1) if match else "utf-8"
    try:
        return data.decode(charset, errors="replace")
    except LookupError:
        return data.decode("utf-8", errors="replace")


def local_fetch(raw_url: str) -> WebFetchResponse:
    requested_url = _normalize_url(raw_url)
    current_url = requested_url
    original_host = urlsplit(requested_url).hostname

    for redirect_count in range(MAX_REDIRECTS + 1):
        pool, response = _open_pinned(current_url)
        try:
            status = response.status
            if status in {301, 302, 303, 307, 308}:
                location = response.headers.get("Location")
                if not location:
                    raise LocalWebError("重定向响应缺少 Location")
                if redirect_count >= MAX_REDIRECTS:
                    raise LocalWebError("网页重定向次数超过上限")
                next_url = _normalize_url(urljoin(current_url, location))
                current = urlsplit(current_url)
                target = urlsplit(next_url)
                if target.hostname != original_host:
                    raise LocalWebError("拒绝跨主机重定向")
                if current.scheme == "https" and target.scheme != "https":
                    raise LocalWebError("拒绝 HTTPS 降级重定向")
                current_url = next_url
                continue
            if status < 200 or status >= 300:
                raise LocalWebError(f"网页返回 HTTP {status}")
            content_type = _check_response_headers(response)
            data = _read_limited(response)
        finally:
            response.release_conn()
            pool.close()
        decoded = _decode_body(data, content_type)
        if "html" in content_type.lower() or "<html" in decoded[:500].lower():
            title, content = extract_llm_text(decoded)
        else:
            title, content = "", _collapse_text(decoded)
        return WebFetchResponse(
            requested_url=requested_url,
            final_url=current_url,
            content=content,
            title=title,
            status=status,
            content_type=content_type,
            retrieved_at=time.time(),
        )
    raise AssertionError("重定向循环应返回或抛出异常")
