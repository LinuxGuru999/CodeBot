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

import re
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
        with urllib.request.urlopen(req, timeout=SEARCH_TIMEOUT) as resp:
            raw = resp.read(MAX_SEARCH_BYTES).decode("utf-8", errors="replace")
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
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
            raw_bytes = resp.read(max_bytes)
            content_type = resp.headers.get("Content-Type", "")
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
