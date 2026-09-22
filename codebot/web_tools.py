#!/usr/bin/env python3
"""Web search and fetch tools for CodeBot agents.

Purpose
-------
Provides internet research capabilities using stdlib baseline with optional enhanced parsing: web_search queries
DuckDuckGo's HTML lite endpoint and parses structured results; web_fetch
retrieves bounded page content from a URL. Both return the uniform
{success, output, error} dict expected by api_runner's tool dispatch.

Why
---
Discovery agents (bug_hunter, security_auditor, dependency_auditor) need
to research CVEs, library APIs, best practices, and known issues beyond
the local codebase. Without search, agents guess at fixes instead of
looking up authoritative references.

Invariants
----------
- stdlib-only baseline (urllib.request, urllib.parse, html.parser, re); uses
  regex-based extraction exclusively -- no third-party runtime dependencies
  (see docs/adr/005-web-tools-html-parsing-strategy.md)
- All I/O bounded: search response capped at 500KB, fetch at 1MB
- Timeouts enforced: 15s for search, 30s for fetch
- Never follows redirects to private IPs (SSRF guard)
- Output truncated to prevent context window explosion
- Fail-open: network errors return error dict, never raise
"""

from __future__ import annotations

import http.client
import ipaddress
import re
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from html import unescape as html_unescape
from html.parser import HTMLParser
from typing import Any

MAX_SEARCH_BYTES = 500_000
MAX_FETCH_BYTES = 1_000_000
SEARCH_TIMEOUT = 15
FETCH_TIMEOUT = 30
MAX_RESULTS = 10
MAX_OUTPUT_CHARS = 8_000

# Secure SSL context with certificate verification enabled.
# Created once at module level to avoid per-connection overhead and ensure
# all HTTPS connections enforce TLS certificate validation.
_SECURE_SSL_CONTEXT = ssl.create_default_context()
_SECURE_SSL_CONTEXT.check_hostname = True
_SECURE_SSL_CONTEXT.verify_mode = ssl.CERT_REQUIRED
_SECURE_SSL_CONTEXT.load_default_certs()

_BLOCKED_PATTERNS = (
    "127.0.0.", "localhost", "::1", "169.254.",
    "10.", "172.16.", "172.17.", "172.18.", "172.19.",
    "172.20.", "172.21.", "172.22.", "172.23.", "172.24.",
    "172.25.", "172.26.", "172.27.", "172.28.", "172.29.",
    "172.30.", "172.31.", "192.168.",
)


class BlockedIPError(ValueError):
    """Raised when host resolves to SSRF-blocked IP.

    Subclass of ValueError for backwards compatibility with existing
    `except ValueError` handlers in callers like _safe_open_url and
    _SSRFRedirectHandler.redirect_request.
    """
    pass


def _parse_ip_literal(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Parse host as an IP literal without using exceptions for control flow.

    Returns the parsed address if *host* is a valid IPv4/IPv6 literal,
    otherwise None (caller proceeds to DNS resolution).

    Only this helper uses try/except around ipaddress.ip_address; callers
    branch on the Optional return value instead of catching ValueError,
    so security decisions never depend on exception-message matching or
    on the ordering of ``except BlockedIPError`` vs ``except ValueError``.
    """
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def _is_blocked_ip(ip_str: str) -> bool:
    """Check if an IP address is private, loopback, link-local, or otherwise unsafe.

    Uses ipaddress module for accurate RFC-compliant checks.
    Blocks: loopback, private, link-local, multicast, unspecified, reserved.

    Args:
        ip_str: IP address string (IPv4 or IPv6).

    Returns:
        True if the IP should be blocked, False if safe for outbound connections.
    """
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        # Invalid IP format - block it to be safe
        return True

    # Block all non-global (private/reserved) addresses
    # is_global returns False for: loopback, private, link-local,
    # multicast, unspecified, reserved, and other special-use ranges
    if not addr.is_global:
        return True

    # Additionally block IPv6 unique local addresses (fc00::/7)
    # which may not be caught by is_global in some Python versions
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.is_private:
            return True

    return False


def _resolve_and_validate_host(host: str, port: int) -> tuple[str, int, int]:
    """Resolve hostname to IP and validate against SSRF blocklist.

    Prevents DNS rebinding attacks by resolving the hostname and checking
    the actual IP address before any connection is made.

    Args:
        host: Hostname or IP address string.
        port: Target port number.

    Returns:
        Tuple of (resolved_ip, port, address_family).

    Raises:
        BlockedIPError: If resolved IP is blocked (SSRF protection).
        ValueError: If DNS resolution fails or host is invalid.
    """
    # Check if host is already an IP address. Branch on the Optional
    # return value instead of using exceptions for control flow: if the
    # helper returns None the host is not an IP literal and we fall
    # through to DNS resolution. Blocked literals raise the distinct
    # BlockedIPError type (a ValueError subclass) -- never matched via
    # substring checks on str(e), so message refactors cannot bypass SSRF.
    addr = _parse_ip_literal(host)
    if addr is not None:
        if _is_blocked_ip(str(addr)):
            raise BlockedIPError(f"resolves to blocked IP: {host}")
        family = socket.AF_INET6 if isinstance(addr, ipaddress.IPv6Address) else socket.AF_INET
        return (str(addr), port, family)

    # Resolve hostname via DNS
    try:
        infos = socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise ValueError(f"DNS resolution failed for {host}: {e}") from e

    if not infos:
        raise ValueError(f"DNS resolution returned no results for {host}")

    # Validate ALL resolved IPs - block if any resolve to private/range
    for info in infos:
        family, socktype, proto, canonname, sockaddr = info
        resolved_ip = sockaddr[0]
        if _is_blocked_ip(resolved_ip):
            raise BlockedIPError(f"{host} resolves to blocked IP: {resolved_ip}")

    # Return first valid resolution
    family, socktype, proto, canonname, sockaddr = infos[0]
    return (sockaddr[0], port, family)


def is_blocked_url(url: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(url)
        host = (parsed.hostname or "").lower()
    except Exception:
        return True
    for pattern in _BLOCKED_PATTERNS:
        if host.startswith(pattern) or host == pattern.rstrip("."):
            return True
    if not host:
        return True
    return False


class _SSRFRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Redirect handler that validates and pins each hop's resolved IP against the SSRF blocklist.

    Prevents DNS rebinding and redirect-based SSRF by resolving, validating,
    and PINNING the target hostname before allowing urllib to follow the redirect.
    """

    def redirect_request(self, req: urllib.request.Request, fp: Any, code: int,
                         msg: str, headers: Any, newurl: str) -> urllib.request.Request | None:
        """Override to validate and pin redirect target before following."""
        # Block via string patterns first (fast path)
        try:
            parsed = urllib.parse.urlparse(newurl)
            host = (parsed.hostname or "").lower()
        except Exception:
            return None

        if not host:
            return None

        for pattern in _BLOCKED_PATTERNS:
            if host.startswith(pattern) or host == pattern.rstrip("."):
                raise ValueError(
                    f"SSRF: redirect to blocked host {host}"
                )

        # Resolve and validate actual IP (DNS rebinding defense)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        try:
            resolved_ip, _, _ = _resolve_and_validate_host(host, port)
        except ValueError as e:
            raise ValueError(f"SSRF: redirect blocked - {e}") from e

        # Create a new request with the pinned IP
        # We reuse the original method and body if applicable
        new_req = urllib.request.Request(newurl)
        new_req._pinned_ip = resolved_ip
        new_req.add_header('Host', host)
        
        # Copy over essential headers from the original request if needed (e.g., cookies, auth)
        # But be careful not to copy the old X-Pinned-IP
        for key, value in req.headers.items():
            new_req.add_header(key, value)

        # All checks passed — allow redirect with pinned IP
        return new_req


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """HTTPConnection that connects to a pre-resolved IP address.

    This prevents DNS rebinding by ensuring the TCP connection is made
    to the exact IP address validated earlier, bypassing any further DNS lookups.
    """
    def __init__(self, host: str, port: int = 80, timeout: float = 15,
                 source_address: tuple[str, int] | None = None,
                 pinned_ip: str | None = None):
        super().__init__(host, port, timeout, source_address)
        self._pinned_ip = pinned_ip

    def connect(self) -> None:
        """Connect to the pinned IP instead of resolving the hostname.
        
        Raises:
            ValueError: If pinned_ip is not provided, preventing fallback to DNS resolution.
        """
        if not self._pinned_ip:
            raise ValueError("Pinned IP is required for secure connection; refusing to resolve hostname")
        
        # Connect directly to the validated IP
        self.sock = socket.create_connection(
            (self._pinned_ip, self.port), self.timeout, self.source_address
        )


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPSConnection that connects to a pre-resolved IP address."""
    def __init__(self, host: str, port: int = 443, timeout: float = 15,
                 source_address: tuple[str, int] | None = None,
                 pinned_ip: str | None = None,
                 context: ssl.SSLContext | None = None):
        if context is None:
            context = _SECURE_SSL_CONTEXT
        super().__init__(
            host,
            port=port,
            timeout=timeout,
            source_address=source_address,
            context=context,
        )
        self._pinned_ip = pinned_ip

    def connect(self) -> None:
        """Connect to the pinned IP and verify SSL against the original hostname.
        
        Raises:
            ValueError: If pinned_ip is missing, empty, whitespace-only, or not a valid IP literal.
        """
        if not self._pinned_ip or not str(self._pinned_ip).strip():
            raise ValueError("Pinned IP is required for secure connection; refusing to resolve hostname")
        
        # Validate that pinned_ip is actually an IP address, never a hostname
        try:
            ipaddress.ip_address(self._pinned_ip)
        except ValueError as e:
            raise ValueError(f"pinned_ip must be a valid IP address, got: {self._pinned_ip!r}") from e
        
        sock = socket.create_connection(
            (self._pinned_ip, self.port), self.timeout, self.source_address
        )
        
        # Handle proxy tunneling correctly via the connection object, not raw socket
        if self._tunnel_host:
            self._tunnel()
        
        # Wrap with SSL, using the original hostname for SNI and verification
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class _PinnedURLHandler(urllib.request.AbstractHTTPHandler):
    """Handler that forces connections to use pre-resolved IPs.

    It extracts the pinned IP from the request's 'X-Pinned-IP' header
    (which we inject internally) and uses the appropriate connection class.
    """
    # Lower handler_order than default HTTPHandler/HTTPSHandler (500)
    # ensures this handler processes requests before defaults, preventing
    # SSRF bypass via handler ordering.
    handler_order = 400

    def http_open(self, req: urllib.request.Request) -> Any:
        return self.do_open(_PinnedHTTPConnection, req, pinned_ip=getattr(req, '_pinned_ip', None))

    def https_open(self, req: urllib.request.Request) -> Any:
        return self.do_open(
            _PinnedHTTPSConnection,
            req,
            pinned_ip=getattr(req, '_pinned_ip', None),
            context=_SECURE_SSL_CONTEXT,
        )


def _safe_open_url(req: urllib.request.Request, timeout: float) -> Any:
    """Open a URL with DNS validation and SSRF-safe redirect handling.

    Steps:
    1. Resolve and validate the target hostname's IP via _resolve_and_validate_host
    2. Inject the resolved IP into the request for the PinnedURLHandler
    3. Use a custom redirect handler that validates and pins each redirect hop
    4. Bounded reads enforced by caller

    Args:
        req: urllib Request object.
        timeout: Connection/read timeout in seconds.

    Returns:
        urllib response object.

    Raises:
        ValueError: If DNS resolution fails or IP is blocked.
        urllib.error.URLError: On network errors.
    """
    parsed = urllib.parse.urlparse(req.full_url)
    host = (parsed.hostname or "").lower()
    port = parsed.port or (443 if parsed.scheme == "https" else 80)

    # Resolve and validate the IP BEFORE connecting (DNS rebinding defense)
    try:
        resolved_ip, _, _ = _resolve_and_validate_host(host, port)
    except ValueError:
        raise

    # Store the pinned IP as a request attribute (not a header) to prevent wire leakage
    req._pinned_ip = resolved_ip
    
    # Ensure the Host header is correct (urllib usually sets this, but we ensure it matches original host)
    if 'Host' not in req.headers:
        req.add_header('Host', host)

    # Build custom opener:
    # 1. PinnedURLHandler: Forces connection to the resolved IP
    # 2. SSRFRedirectHandler: Validates and pins IPs for redirects
    opener = urllib.request.build_opener(_PinnedURLHandler, _SSRFRedirectHandler)
    return opener.open(req, timeout=timeout)


class _DDGParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.results: list[dict[str, str]] = []
        self._in_result = False
        self._current: dict[str, str] = {}
        self._capture = ""
        self._capturing = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_dict = {k: v for k, v in attrs if v is not None}
        cls = attr_dict.get("class", "")
        if tag == "a" and "result__a" in cls:
            href = attr_dict.get("href", "")
            if href.startswith("//duckduckgo.com/l/?uddg="):
                parsed = urllib.parse.urlparse(href)
                qs = urllib.parse.parse_qs(parsed.query)
                actual = qs.get("uddg", [href])[0]
                self._current["url"] = actual
            elif href.startswith("http"):
                self._current["url"] = href
            self._capturing = True
            self._capture = ""
        if tag == "a" and "result__snippet" in cls:
            self._capturing = True
            self._capture = ""

    def handle_data(self, data: str) -> None:
        if self._capturing:
            self._capture += data

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._capturing:
            text = self._capture.strip()
            if text:
                if "url" in self._current and "title" not in self._current:
                    self._current["title"] = text[:200]
                elif "url" in self._current and "title" in self._current:
                    self._current["snippet"] = text[:500]
                    self.results.append(self._current)
                    self._current = {}
            self._capturing = False
            self._capture = ""


def web_search(query: str, max_results: int = MAX_RESULTS) -> dict[str, Any]:
    """Search DuckDuckGo and return structured results.

    Args:
        query: Search query string.
        max_results: Maximum number of results to return (default 10).

    Returns:
        Dict with keys success, output, error. Never raises.
    """
    if not query or not query.strip():
        return {"success": False, "output": "", "error": "empty query"}

    encoded = urllib.parse.quote_plus(query.strip()[:500])
    url = f"https://html.duckduckgo.com/html/?q={encoded}"

    if is_blocked_url(url):
        return {"success": False, "output": "", "error": "blocked URL"}

    req = urllib.request.Request(url, headers={
        "User-Agent": "CodeBot/0.2 (autonomous engineering agent)",
        "Accept": "text/html",
    })

    try:
        resp = _safe_open_url(req, timeout=SEARCH_TIMEOUT)
        try:
            raw = resp.read(MAX_SEARCH_BYTES).decode("utf-8", errors="replace")
        finally:
            resp.close()
    except ValueError as e:
        return {"success": False, "output": "", "error": f"SSRF blocked: {str(e)[:150]}"}
    except urllib.error.HTTPError as e:
        return {"success": False, "output": "", "error": f"HTTP {e.code}"}
    except urllib.error.URLError as e:
        return {"success": False, "output": "", "error": f"network error: {str(e.reason)[:100]}"}
    except TimeoutError:
        return {"success": False, "output": "", "error": "search timed out"}
    except Exception as e:
        return {"success": False, "output": "", "error": str(e)[:200]}

    parser = _DDGParser()
    try:
        parser.feed(raw)
    except Exception:
        pass

    results = parser.results[:max_results]
    if not results:
        fallback = re.findall(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', raw, re.DOTALL)
        for href, title in fallback[:max_results]:
            clean_title = re.sub(r'<[^>]+>', '', title).strip()
            if clean_title and href.startswith("http"):
                results.append({"url": href, "title": clean_title[:200], "snippet": ""})

    if not results:
        return {"success": True, "output": "No results found.", "error": None}

    lines: list[str] = []
    for i, r in enumerate(results, 1):
        title = r.get("title", "Untitled")
        url = r.get("url", "")
        snippet = r.get("snippet", "")
        entry = f"[{i}] {title}\n    URL: {url}"
        if snippet:
            entry += f"\n    {snippet}"
        lines.append(entry)

    output = "\n\n".join(lines)
    if len(output) > MAX_OUTPUT_CHARS:
        output = output[:MAX_OUTPUT_CHARS] + "\n...[truncated]"

    return {"success": True, "output": output, "error": None}


def web_fetch(url: str, max_bytes: int = MAX_FETCH_BYTES) -> dict[str, Any]:
    """Fetch and extract readable text from a URL.

    Args:
        url: URL to fetch. Must be http/https.
        max_bytes: Maximum bytes to read (default 1MB).

    Returns:
        Dict with keys success, output, error. Never raises.
    """
    if not url or not url.strip():
        return {"success": False, "output": "", "error": "empty URL"}

    url = url.strip()
    if not url.startswith(("http://", "https://")):
        return {"success": False, "output": "", "error": "only http/https URLs allowed"}

    if is_blocked_url(url):
        return {"success": False, "output": "", "error": "blocked URL (private/local address)"}

    req = urllib.request.Request(url, headers={
        "User-Agent": "CodeBot/0.2 (autonomous engineering agent)",
        "Accept": "text/html,text/plain,application/json",
    })

    try:
        resp = _safe_open_url(req, timeout=FETCH_TIMEOUT)
        try:
            raw_bytes = resp.read(max_bytes)
            content_type = resp.headers.get("Content-Type", "")
        finally:
            resp.close()
    except ValueError as e:
        return {"success": False, "output": "", "error": f"SSRF blocked: {str(e)[:150]}"}
    except urllib.error.HTTPError as e:
        return {"success": False, "output": "", "error": f"HTTP {e.code}"}
    except urllib.error.URLError as e:
        return {"success": False, "output": "", "error": f"network error: {str(e.reason)[:100]}"}
    except TimeoutError:
        return {"success": False, "output": "", "error": "fetch timed out"}
    except Exception as e:
        return {"success": False, "output": "", "error": str(e)[:200]}

    raw_text = raw_bytes.decode("utf-8", errors="replace")

    if "json" in content_type or url.endswith(".json"):
        text = raw_text[:MAX_OUTPUT_CHARS]
    elif "html" in content_type or url.endswith((".html", ".htm")):
        text = extract_text_from_html(raw_text)
    else:
        text = raw_text[:MAX_OUTPUT_CHARS]

    text = re.sub(r'\n{3,}', '\n\n', text).strip()
    if len(text) > MAX_OUTPUT_CHARS:
        text = text[:MAX_OUTPUT_CHARS] + "\n...[truncated]"

    if not text:
        return {"success": False, "output": "", "error": "empty response body"}

    return {"success": True, "output": text, "error": None}


class _FallbackStripper(HTMLParser):
    """Stdlib HTMLParser that strips script/style/nav/footer/header/aside content.

    Handles malformed or unclosed tags by tracking skip depth; content inside
    an unclosed skip-tag is discarded until parser.close().
    """

    SKIP_TAGS = frozenset({"script", "style", "nav", "footer", "header", "aside"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self._skip_depth = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() in self.SKIP_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in self.SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._parts.append(data)

    def handle_entityref(self, name: str) -> None:
        if self._skip_depth == 0:
            self._parts.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if self._skip_depth == 0:
            self._parts.append(f"&#{name};")

    def get_text(self) -> str:
        return "".join(self._parts)


def extract_text_from_html(html: str) -> str:
    """Extract readable text from HTML using stdlib HTMLParser.

    Uses a custom HTMLParser subclass to robustly strip script/style and other
    non-content tags, including malformed or unclosed variants that defeat
    naive regex approaches.

    Args:
        html: Raw HTML string.

    Returns:
        Extracted plain text.
    """
    stripper = _FallbackStripper()
    try:
        stripper.feed(html)
        stripper.close()
    except Exception:
        # Fail open to empty rather than leaking raw HTML/script content
        return ""

    text = stripper.get_text()

    # Normalize whitespace and decode common entities post-strip
    # Decode all HTML entities (named + numeric) via stdlib html.unescape.
    # This replaces manual regex substitutions and correctly handles &#65; -> 'A'
    # instead of stripping numeric refs to empty string.
    text = html_unescape(text)
    text = re.sub(r'[ \t]+', ' ', text)
    lines = [line.strip() for line in text.splitlines()]
    lines = [line for line in lines if line]
    return '\n'.join(lines)


_extract_text_from_html = extract_text_from_html
