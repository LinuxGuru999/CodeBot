# web_tools.py

Provides stdlib-only internet research capabilities: web_search queries DuckDuckGo's HTML lite endpoint and parses structured results; web_fetch retrieves bounded page content from a URL. Both return the uniform {success, output, error} dict expected by api_runner's tool dispatch.

## Key Exports
- `extract_text_from_html()`: Function
- `is_blocked_url()`: Function
- `web_search()`: Function
- `web_fetch()`: Function
- `handle_starttag()`: Function
- `handle_data()`: Function
- `handle_endtag()`: Function

## Invariants
- stdlib-only baseline (urllib.request, urllib.parse, html.parser, re); optionally uses
  beautifulsoup4 + lxml for robust HTML parsing when installed, otherwise falls back
  to regex-based extraction -- no required third-party runtime dependencies
  (see docs/adr/005-web-tools-html-parsing-strategy.md)
- All I/O bounded: search response capped at 500KB, fetch at 1MB
- Timeouts enforced: 15s for search, 30s for fetch
- Never follows redirects to private IPs (SSRF guard)
- Output truncated to prevent context window explosion
- Fail-open: network errors return error dict, never raise
