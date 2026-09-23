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
- stdlib-only (urllib.request, urllib.parse, html.parser, re, html.unescape)
- No third-party runtime dependencies (beautifulsoup4, lxml removed per CB-4080995-C643)
- All I/O bounded: search response capped at 500KB, fetch at 1MB
- Timeouts enforced: 15s for search, 30s for fetch
- Never follows redirects to private IPs (SSRF guard)
- Output truncated to prevent context window explosion
- Fail-open: network errors return error dict, never raise
