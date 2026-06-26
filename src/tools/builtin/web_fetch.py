from __future__ import annotations

import re
import ipaddress
import socket
import urllib.error
import urllib.parse
import urllib.request
from html import unescape
from html.parser import HTMLParser

from pydantic import BaseModel, Field

from src.tools.decorator import ToolPermission, tool


DEFAULT_MAX_BYTES = 1_000_000
REQUEST_TIMEOUT_SECONDS = 15
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)
READABLE_CONTENT_TYPES = {
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
}
SENSITIVE_QUERY_KEYS = {
    "access_token",
    "api_key",
    "apikey",
    "auth",
    "authorization",
    "code",
    "credential",
    "key",
    "password",
    "secret",
    "signature",
    "sig",
    "token",
}


class WebFetchInput(BaseModel):
    url: str = Field(..., description="要访问的 http/https URL。")


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
        elif tag in {"p", "div", "section", "article", "li"}:
            self.text_parts.append("\n")
        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self.text_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self.title_parts.append(data)
            return
        self.text_parts.append(data)


def _collapse_text(text: str) -> str:
    lines = []
    for line in text.splitlines():
        normalized = re.sub(r"[ \t\r\f\v]+", " ", line).strip()
        if normalized:
            lines.append(normalized)
    return "\n".join(lines).strip()


def extract_llm_text(html: str) -> tuple[str, str]:
    parser = _ReadableHTMLParser()
    parser.feed(html)
    title = _collapse_text(" ".join(parser.title_parts))
    text = _collapse_text(unescape("".join(parser.text_parts)))
    return title, text


def validate_http_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url.strip())
    if parsed.scheme not in {"http", "https"}:
        return "错误：仅支持 http/https URL。"
    if not parsed.netloc:
        return "错误：URL 缺少主机名。"
    if _is_private_host(parsed.hostname or ""):
        return "错误：隐私保护，拒绝访问本机、内网或云元数据地址。"
    return urllib.parse.urlunparse(parsed)


def resolve_public_ips(hostname: str) -> list[str] | str:
    if _is_private_host(hostname):
        return "错误：隐私保护，拒绝访问本机、内网或云元数据地址。"
    try:
        infos = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        return f"访问失败: DNS 解析失败: {e}"

    addresses = sorted({info[4][0] for info in infos})
    if not addresses:
        return "访问失败: DNS 未返回可用地址。"
    for address in addresses:
        if _is_private_host(address):
            return "错误：隐私保护，目标域名解析到本机、内网或云元数据地址。"
    return addresses


def validate_public_url(url: str) -> str:
    normalized = validate_http_url(url)
    if normalized.startswith("错误"):
        return normalized
    parsed = urllib.parse.urlparse(normalized)
    resolved = resolve_public_ips(parsed.hostname or "")
    if isinstance(resolved, str):
        return resolved
    return normalized


def _is_private_host(hostname: str) -> bool:
    host = hostname.strip("[]").lower()
    if host in {"localhost", "metadata.google.internal"}:
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def redact_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    redacted_query = [
        (key, "[REDACTED]" if key.lower() in SENSITIVE_QUERY_KEYS else value)
        for key, value in query
    ]
    encoded_query = urllib.parse.urlencode(redacted_query, doseq=True).replace(
        "%5BREDACTED%5D",
        "[REDACTED]",
    )
    return urllib.parse.urlunparse(parsed._replace(
        query=encoded_query,
    ))


def should_skip_response(
    content_type: str,
    content_disposition: str,
    content_length: str | None,
) -> str | None:
    disposition = content_disposition.lower()
    if "attachment" in disposition:
        return "访问被跳过: 响应是附件下载，web_fetch 只提取网页或文本内容。"

    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type and not (
        media_type.startswith("text/")
        or media_type in READABLE_CONTENT_TYPES
        or media_type.endswith("+json")
        or media_type.endswith("+xml")
    ):
        return f"访问被跳过: 不支持的内容类型 {media_type}，web_fetch 只提取网页或文本内容。"

    if content_length:
        try:
            size = int(content_length)
        except ValueError:
            size = 0
        if size > DEFAULT_MAX_BYTES:
            return f"访问被跳过: 内容过大 ({size} bytes)，当前上限为 {DEFAULT_MAX_BYTES} bytes。"

    return None


def format_fetch_result(
    url: str,
    final_url: str,
    status: int,
    content_type: str,
    title: str,
    content: str,
) -> str:
    title_line = title or "无标题"
    return "\n".join([
        "网页内容:",
        f"URL: {redact_url(url)}",
        f"最终URL: {redact_url(final_url)}",
        f"状态码: {status}",
        f"Content-Type: {content_type or '未知'}",
        f"标题: {title_line}",
        "",
        "正文:",
        content or "未提取到可读正文。",
    ])


def _decode_body(data: bytes, content_type: str) -> str:
    charset_match = re.search(r"charset=([\w.-]+)", content_type, re.IGNORECASE)
    charset = charset_match.group(1) if charset_match else "utf-8"
    try:
        return data.decode(charset, errors="replace")
    except LookupError:
        return data.decode("utf-8", errors="replace")


@tool(model=WebFetchInput, description="访问指定URL，返回网页正文内容。",
      permission=ToolPermission(kind="readonly", specifier_arg="url", tips="访问网页：{url}"))
def web_fetch(url: str) -> str:
    normalized_url = validate_public_url(url)
    if normalized_url.startswith(("错误", "访问失败")):
        return normalized_url

    request = urllib.request.Request(
        normalized_url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html, text/plain, application/xhtml+xml;q=0.9",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            final_url = response.geturl()
            final_url_check = validate_public_url(final_url)
            if final_url_check.startswith(("错误", "访问失败")):
                return final_url_check
            status = response.status
            content_type = response.headers.get("Content-Type", "")
            content_disposition = response.headers.get("Content-Disposition", "")
            content_length = response.headers.get("Content-Length")
            skip_reason = should_skip_response(content_type, content_disposition, content_length)
            if skip_reason:
                return skip_reason
            data = response.read(DEFAULT_MAX_BYTES + 1)
            if len(data) > DEFAULT_MAX_BYTES:
                return f"访问被跳过: 内容过大，当前上限为 {DEFAULT_MAX_BYTES} bytes。"
    except urllib.error.HTTPError as e:
        return f"访问失败: HTTP {e.code} {e.reason}"
    except urllib.error.URLError as e:
        return f"访问失败: {e.reason}"
    except TimeoutError:
        return "访问失败: 请求超时"

    decoded = _decode_body(data, content_type)
    if "html" in content_type.lower() or "<html" in decoded[:500].lower():
        title, content = extract_llm_text(decoded)
    else:
        title = ""
        content = _collapse_text(decoded)

    return format_fetch_result(
        url=normalized_url,
        final_url=final_url,
        status=status,
        content_type=content_type,
        title=title,
        content=content,
    )
