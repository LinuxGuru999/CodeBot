# ADR 005: Web Tools HTML Parsing — Dependency Strategy

## Status
Accepted

## Context

`web_tools.py` provides HTML parsing in `extract_text_from_html()` (lines 506–614) via a 3-tier strategy:

1. **Try `beautifulsoup4` + `lxml`** — imported inside the function body (`from bs4 import BeautifulSoup`), wrapped in `try/except ImportError: pass`.
2. **Try `lxml` directly** — similarly imported inside a `try/except ImportError: pass`.
3. **Stdlib regex fallback** — uses `re.sub` to strip tags and entities, always available.

This creates a **documentary mismatch**: the module docstring (line 19) and `docs/modules/web_tools.md` (line 13) both declare a strict `stdlib-only` invariant, while the code already performs optional imports of `bs4` and `lxml` at runtime. No `pyproject.toml` dependency is declared for either library.

Meanwhile, `.codebot/project.yaml` enforces a `stdlib-only` policy with `allowed_third_party` limited to dev-only tools (`pytest`, `pytest-c-cov`).

The question is whether to:

**(a)** Remove the `bs4`/`lxml` optional imports and rely solely on stdlib parsing, fully aligning code with the stated policy.

**(b)** Formally declare `bs4` and `lxml` as required (or optional) dependencies in `pyproject.toml`, and update documentation to reflect this.

## Decision

**Retain `bs4`/`lxml` as optional, non-required enhancements** and correct the documentation to match the actual runtime behavior. Specifically:

- **No change to `pyproject.toml`** — dependencies remain `[]` at runtime. `bs4` and `lxml` are not declared as required or optional extras.
- **No code changes** — the existing `try/except ImportError` pattern already implements correct optional-dependency semantics. If a user or CI environment installs `bs4`/`lxml`, parsing quality improves automatically. If not, the regex fallback handles it.
- **Documentation updated** — the module docstring and `docs/modules/web_tools.md` are corrected to say *"`stdlib-only` baseline with optional `bs4`/`lxml` enhancement"* rather than the currently inaccurate strict `stdlib-only` claim.
- **This ADR** serves as the recorded rationale.

## Tradeoffs

| Dimension | Stdlib-only (a) | Optional bs4/lxml (chosen, b-lite) | Required bs4/lxml (b-full, **not chosen**) |
|---|---|---|---|
| **Install burden** | Zero — no third-party packages needed | Zero at baseline; improvement if user opts in | Forced install of `beautifulsoup4`, `lxml` (C extension) |
| **Supply-chain risk** | None | None at baseline | Adds `lxml` (C/ctypes, historical CVEs) to attack surface |
| **Policy compliance** | Fully complies with `project.yaml` stdlib-only | Complies — no deps declared, no policy violation | Violates `project.yaml` stdlib-only; requires policy change |
| **Malformed HTML handling** | Regex-based, brittle on edge cases (nested tags, unclosed tags, CDATA) | Robust when bs4/lxml available; graceful degradation otherwise | Consistently robust |
| **Portability** | Runs everywhere Python runs | Runs everywhere; better on capable environments | Requires C compiler for `lxml` on some platforms |
| **Documentation accuracy** | Matches current docstring (but not code) | Requires docstring update (this ADR) | Requires docstring + pyproject.toml + project.yaml updates |

## Consequences

1. **`pyproject.toml` is not modified.** No new runtime dependencies are declared. The packaging story remains zero third-party deps.
2. **`codebot/web_tools.py` docstring** is updated to accurately describe the optional-enhancement behavior. See line 19–20 change.
3. **`docs/modules/web_tools.md`** is updated to match.
4. **CI environments** that install `bs4`/`lxml` for testing get better parsing. Environments that don't are unaffected — the regex fallback continues to work.
5. **Future work** may consider:
   - Adding a `logger.debug()` when the `ImportError` fallback triggers, for observability.
   - Publishing a `[full]` extras group in `pyproject.toml` (e.g., `pip install codebot[full]`) if broad adoption of enhanced parsing is desired. This would be a separate ADR.
6. **No follow-up implementation ticket is needed** for this decision — the code already implements the chosen path correctly. This ADR is documentation-only.

## References

- `codebot/web_tools.py` lines 506–614 (extract_text_from_html)
- `pyproject.toml` — `dependencies = []`
- `.codebot/project.yaml` — `dependencies.policy: stdlib-only`
- `docs/adr/004-agent-company-id.md` — ADR template/structure
