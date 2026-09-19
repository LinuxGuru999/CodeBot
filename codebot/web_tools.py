#!/usr/bin/env python3
"""Web search and fetch tools for CodeBot agents.

Purpose
-------
Provides stdlib-only internet research capabilities: web_search queries
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
- stdlib-only (urllib.request, urllib.parse, html.parser, re)
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
from html.parser import HTMLParser
from typing import Any

MAX_SEARCH_BYTES = 500_000
MAX_FETCH_BYTES = 1_000_000
SEARCH_TIMEOUT = 15
FETCH_TIMEOUT = 30
MAX_RESULTS = 10
MAX_OUTPUT_CHARS = 8_000

_BLOCKED_PATTERNS = (
    "127.0.0.", "localhost", "::1", "169.254.",
    "10.", "172.16.", "172.17.", "172.18.", "172.19.",
    "172.20.", "172.21.", "172.22.", "172.23.", "172.24.",
    "172.25.", "172.26.", "172.27.", "172.28.", "172.29.",
    "172.30.", "172.31.", "192.168.",
)


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
        ValueError: If DNS resolution fails or resolved IP is blocked.
    """
    # Check if host is already an IP address
    try:
        addr = ipaddress.ip_address(host)
        if _is_blocked_ip(str(addr)):
            raise ValueError(f"resolves to blocked IP: {host}")
        family = socket.AF_INET6 if isinstance(addr, ipaddress.IPv6Address) else socket.AF_INET
        return (str(addr), port, family)
    except ValueError as e:
        if "blocked IP" in str(e):
            raise
        # Not an IP address, proceed with DNS resolution

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
            raise ValueError(f"{host} resolves to blocked IP: {resolved_ip}")

    # Return first valid resolution
    family, socktype, proto, canonname, sockaddr = infos[0]
    return (sockaddr[0], port, family)


def _is_blocked_url(url: str) -> bool:
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
        new_req.add_header('X-Pinned-IP', resolved_ip)
        new_req.add_header('Host', host)
        
        # Copy over essential headers from the original request if needed (e.g., cookies, auth)
        # But be careful not to copy the old X-Pinned-IP
        for key, value in req.headers.items():
            if key.lower() != 'x-pinned-ip':
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
        super().__init__(host, port, timeout, source_address, context=context)
        self._pinned_ip = pinned_ip

    def connect(self) -> None:
        """Connect to the pinned IP and verify SSL against the original hostname."""
        if self._pinned_ip:
            sock = socket.create_connection(
                (self._pinned_ip, self.port), self.timeout, self.source_address
            )
            if self._tunnel_host:
                sock.set_tunnel(self._tunnel_host, self._tunnel_port)
            
            # Wrap with SSL, using the original hostname for SNI and verification
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        else:
            super().connect()


class _PinnedURLHandler(urllib.request.AbstractHTTPHandler):
    """Handler that forces connections to use pre-resolved IPs.

    It extracts the pinned IP from the request's 'X-Pinned-IP' header
    (which we inject internally) and uses the appropriate connection class.
    """
    def http_open(self, req: urllib.request.Request) -> Any:
        return self.do_open(_PinnedHTTPConnection, req, pinned_ip=req.headers.get('X-Pinned-IP'))

    def https_open(self, req: urllib.request.Request) -> Any:
        return self.do_open(_PinnedHTTPSConnection, req, pinned_ip=req.headers.get('X-Pinned-IP'))


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

    # Inject the pinned IP into the request headers for the handler to pick up
    # We use a custom header that will be stripped/used internally, not sent to the server
    req.add_header('X-Pinned-IP', resolved_ip)
    
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

    if _is_blocked_url(url):
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

    if _is_blocked_url(url):
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
        text = _extract_text_from_html(raw_text)
    else:
        text = raw_text[:MAX_OUTPUT_CHARS]

    text = re.sub(r'\n{3,}', '\n\n', text).strip()
    if len(text) > MAX_OUTPUT_CHARS:
        text = text[:MAX_OUTPUT_CHARS] + "\n...[truncated]"

    if not text:
        return {"success": False, "output": "", "error": "empty response body"}

    return {"success": True, "output": text, "error": None}


def _extract_text_from_html(html: str) -> str:
    for tag in ("script", "style", "nav", "footer", "header", "aside"):
        html = re.sub(rf'<{tag}[^>]*>.*?</{tag}>', ' ', html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r'<br\s*/?>', '\n', html, flags=re.IGNORECASE)
    html = re.sub(r'</p>', '\n\n', html, flags=re.IGNORECASE)
    html = re.sub(r'</h[1-6]>', '\n\n', html, flags=re.IGNORECASE)
    html = re.sub(r'</li>', '\n', html, flags=re.IGNORECASE)
    html = re.sub(r'<[^>]+>', ' ', html)
    html = re.sub(r'&nbsp;', ' ', html)
    html = re.sub(r'&amp;', '&', html)
    html = re.sub(r'&lt;', '<', html)
    html = re.sub(r'&gt;', '>', html)
    html = re.sub(r'&#\d+;', '', html)
    html = re.sub(r'[ \t]+', ' ', html)
    lines = [line.strip() for line in html.splitlines()]
    lines = [line for line in lines if line]
    return '\n'.join(lines)
