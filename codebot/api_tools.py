"""CodeBot API Tools — minimal stdlib-only tools for the API runner.

Purpose
-------
Provides synchronous, fail-open tools (read, write, edit, bash, grep, glob,
a11y_snapshot) for the CodeBot API runner. Each tool returns a uniform dict
{success, output, error} so the runner loop never needs try/except. The a11y_snapshot
tool extracts Playwright accessibility trees as structured text for UI verification.

Why
---
Agent-executed tools must be safe without third-party dependencies: bounded I/O
prevents unbounded reads or hangs (1 MB read cap, 10 KB grep cap, 100 KB bash
cap, bash timeout). write is atomic via tmp+replace to avoid mid-write
corruption if the runner crashes, mirroring orchestrator._write_json_atomic.
edit performs surgical string replacement (old_string → new_string) so bots can
modify source files without rewriting entire files. Fail-open dict returns keep
the runner resilient to missing files, bad regex, or timed-out commands.
The screenshot tool shells out to Playwright Chromium for headless page capture,
enabling visual QA verification by omni-modal models (qwen-3.5-omni-plus).

Invariants
----------
- stdlib-only: pathlib, subprocess, re, json, logging, tempfile, base64
- Every tool returns exactly {success: bool, output: str, error: str|None} and never raises
- a11y_snapshot returns structured JSON text for UI verification via standard tool results
- All I/O is bounded: read 1 MB, bash 100 KB + timeout, grep 10 KB, screenshot 2 MB
- write is atomic via tmp+replace
- edit fails if old_string not found or found multiple times (no ambiguity)
- No async, no LSP/Git/web tools beyond the seven listed
"""

import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path

try:
    from codebot.tool_policy import allowlisted_command, resolve_workspace_path
except ImportError:
    try:
        from tool_policy import allowlisted_command, resolve_workspace_path
    except ImportError:
        def allowlisted_command(command: str) -> list[str] | None:
            import shlex
            try:
                return shlex.split(command)
            except ValueError:
                return None

        def resolve_workspace_path(path: str, workspace_root) -> "Path | None":
            from pathlib import Path
            candidate = Path(path)
            resolved = (candidate if candidate.is_absolute() else Path(workspace_root) / candidate).resolve(strict=False)
            try:
                resolved.relative_to(workspace_root)
            except ValueError:
                return None
            return resolved

logger = logging.getLogger(__name__)

MAX_READ_BYTES = 1_000_000
MAX_BASH_OUTPUT = 100_000
MAX_GREP_OUTPUT = 10_000
MAX_SCREENSHOT_BYTES = 2_000_000
WORKSPACE_ROOT = Path(os.getenv("BOT_WORKSPACE_ROOT", str(Path(__file__).parent.parent))).resolve()


def read(path, offset=None, limit=None):
    """Read file contents with a 1 MB cap and optional line slicing.

    Args:
        path: File to read.
        offset: Optional 0-based line offset to start from.
        limit: Optional max lines to return after offset.

    Returns:
        Dict with keys success, output, error. Never raises.
    """
    try:
        p = resolve_workspace_path(path, WORKSPACE_ROOT)
        if p is None:
            return {"success": False, "output": "", "error": "path denied"}
        # Bounded read: never load more than 1 MB even if file is larger
        with p.open("rb") as fh:
            raw = fh.read(MAX_READ_BYTES)
        text = raw.decode("utf-8", errors="replace")
        if offset is not None or limit is not None:
            lines = text.splitlines()
            start = offset if offset is not None else 0
            if start < 0:
                start = 0
            if limit is not None:
                text = "\n".join(lines[start:start + limit])
            else:
                text = "\n".join(lines[start:])
        return {"success": True, "output": text, "error": None}
    except Exception as exc:
        # Fail-open: caller handles dict, not exception
        return {"success": False, "output": "", "error": str(exc)}


def write(path, content):
    """Write file atomically via tmp+replace.

    Args:
        path: Destination file path.
        content: Text content to write.

    Returns:
        Dict with keys success, output, error. Never raises.
    """
    try:
        p = resolve_workspace_path(path, WORKSPACE_ROOT)
        if p is None:
            return {"success": False, "output": "", "error": "path denied"}
        # Ensure parent exists: API runner may write to new subdirectories
        p.parent.mkdir(parents=True, exist_ok=True)
        # Atomic tmp+replace: prevents mid-write corruption if process crashes
        tmp = Path(str(p) + ".tmp")
        tmp.write_text(content if isinstance(content, str) else str(content), encoding="utf-8")
        tmp.replace(p)
        size = len(content) if isinstance(content, str) else len(str(content))
        return {"success": True, "output": f"Wrote {size} bytes to {path}", "error": None}
    except Exception as exc:
        return {"success": False, "output": "", "error": str(exc)}


def bash(command, timeout=30):
    """Run shell command with timeout and bounded output.

    Args:
        command: Shell command string.
        timeout: Seconds before forced termination.

    Returns:
        Dict with keys success, output, error. Never raises.
    """
    try:
        argv = allowlisted_command(command)
        if argv is None:
            import logging as _log
            _log.getLogger(__name__).warning("bash denied: %s", command[:200])
            return {"success": False, "output": "", "error": "command denied"}
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=WORKSPACE_ROOT,
        )
        # Combine stdout and stderr so caller sees full output in one field
        combined = (result.stdout or "") + (result.stderr or "")
        if len(combined) > MAX_BASH_OUTPUT:
            combined = combined[:MAX_BASH_OUTPUT] + "\n[truncated at 100KB]"
        success = result.returncode == 0
        error = None
        if not success:
            # Prefer stderr as error signal, fall back to exit code
            error = (result.stderr or "").strip() or f"exit code {result.returncode}"
        return {"success": success, "output": combined, "error": error}
    except subprocess.TimeoutExpired as exc:
        partial = ""
        if exc.stdout:
            partial += exc.stdout if isinstance(exc.stdout, str) else exc.stdout.decode("utf-8", errors="replace")
        if exc.stderr:
            partial += exc.stderr if isinstance(exc.stderr, str) else exc.stderr.decode("utf-8", errors="replace")
        if len(partial) > MAX_BASH_OUTPUT:
            partial = partial[:MAX_BASH_OUTPUT] + "\n[truncated at 100KB]"
        return {"success": False, "output": partial, "error": f"timeout after {timeout}s"}
    except Exception as exc:
        return {"success": False, "output": "", "error": str(exc)}


def grep(pattern, path, include=None):
    """Search files by regex with capped output.

    Args:
        pattern: Regular expression to search for.
        path: File or directory to search.
        include: Optional glob filter (e.g. "*.py").

    Returns:
        Dict with keys success, output, error. Never raises.
    """
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        return {"success": False, "output": "", "error": f"invalid regex: {exc}"}
    try:
        base = resolve_workspace_path(path, WORKSPACE_ROOT)
        if base is None:
            return {"success": False, "output": "", "error": "path denied"}
        if not base.exists():
            return {"success": False, "output": "", "error": f"path not found: {path}"}
        files: list[Path] = []
        if base.is_file():
            # Honor include filter even for single file
            if include and not (base.match(include) or base.match(f"**/{include}")):
                return {"success": True, "output": "", "error": None}
            files = [base]
        else:
            # Recursive walk: rglob finds all files under directory
            for p in base.rglob("*"):
                if not p.is_file() or resolve_workspace_path(str(p), WORKSPACE_ROOT) is None:
                    continue
                if include and not (p.match(include) or p.match(f"**/{include}")):
                    continue
                files.append(p)
        matches: list[str] = []
        total = 0
        for fpath in files:
            try:
                # Bounded per-file read to avoid loading huge files unbounded
                with fpath.open("rb") as fh:
                    raw = fh.read(MAX_READ_BYTES)
                text = raw.decode("utf-8", errors="replace")
            except Exception:
                # Skip unreadable files: grep is fail-open per-file
                continue
            for idx, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    entry = f"{fpath}:{idx}:{line}"
                    entry_len = len(entry) + 1
                    if total + entry_len > MAX_GREP_OUTPUT:
                        remaining = MAX_GREP_OUTPUT - total
                        if remaining > 0:
                            matches.append(entry[:remaining])
                        total = MAX_GREP_OUTPUT
                        break
                    matches.append(entry)
                    total += entry_len
            if total >= MAX_GREP_OUTPUT:
                break
        output = "\n".join(matches)
        if len(output) > MAX_GREP_OUTPUT:
            output = output[:MAX_GREP_OUTPUT]
        return {"success": True, "output": output, "error": None}
    except Exception as exc:
        return {"success": False, "output": "", "error": str(exc)}


def edit(path, old_string, new_string):
    """Surgically replace old_string with new_string in a file.

    Fails if old_string is not found or found multiple times (no ambiguity).
    Writes atomically via tmp+replace.

    Args:
        path: File to edit.
        old_string: Exact string to find and replace.
        new_string: Replacement string.

    Returns:
        Dict with keys success, output, error. Never raises.
    """
    try:
        p = resolve_workspace_path(path, WORKSPACE_ROOT)
        if p is None:
            return {"success": False, "output": "", "error": "path denied"}
        if not p.exists():
            return {"success": False, "output": "", "error": f"file not found: {path}"}
        text = p.read_text(encoding="utf-8")
        count = text.count(old_string)
        if count == 0:
            return {"success": False, "output": "", "error": f"old_string not found in {path}"}
        if count > 1:
            return {"success": False, "output": "", "error": f"old_string found {count} times in {path} — must be unambiguous"}
        new_text = text.replace(old_string, new_string, 1)
        # Atomic write via tmp+replace
        tmp = Path(str(p) + ".tmp")
        tmp.write_text(new_text, encoding="utf-8")
        tmp.replace(p)
        return {"success": True, "output": f"Edited {path} ({count} replacement)", "error": None}
    except Exception as exc:
        return {"success": False, "output": "", "error": str(exc)}


def glob(pattern, path="."):
    """Find files matching a glob pattern recursively.

    Args:
        pattern: Glob pattern (e.g. "**/*.py", "*.md").
        path: Root directory to search from (default: current dir).

    Returns:
        Dict with keys success, output (newline-separated paths), error. Never raises.
    """
    try:
        base = resolve_workspace_path(path, WORKSPACE_ROOT)
        if base is None:
            return {"success": False, "output": "", "error": "path denied"}
        if not base.exists():
            return {"success": False, "output": "", "error": f"path not found: {path}"}
        matches = sorted(
            str(p) for p in base.rglob(pattern)
            if p.is_file() and resolve_workspace_path(str(p), WORKSPACE_ROOT) is not None
        )
        # Cap output at 10KB to prevent huge listings
        output = "\n".join(matches)
        if len(output) > MAX_GREP_OUTPUT:
            output = output[:MAX_GREP_OUTPUT] + "\n[truncated]"
        return {"success": True, "output": output, "error": None}
    except Exception as exc:
        return {"success": False, "output": "", "error": str(exc)}


def a11y_snapshot(url, output_path=None, viewport_width=1280, viewport_height=900, wait_ms=1000):
    """Extract Playwright accessibility tree from a rendered page.

    Returns structured text representation of the page's accessibility tree,
    suitable for LLM analysis via standard text messages (no vision required).
    Catches structural regressions: missing elements, wrong ARIA roles, broken
    navigation hierarchy, absent workspace topbar items.

    Uses Node.js Playwright + system Chromium via locator.ariaSnapshot().
    Fail-open: returns error dict if Playwright unavailable or capture fails.

    Args:
        url: Page URL to analyze (e.g., http://127.0.0.1:PORT/#workspace=monitoring).
        output_path: Optional file path to save snapshot text.
        viewport_width: Browser viewport width in pixels.
        viewport_height: Browser viewport height in pixels.
        wait_ms: Milliseconds to wait after navigation for JS rendering.

    Returns:
        Dict with keys success, output, error. Never raises.
    """
    out_arg = str(output_path) if output_path else ""
    node_script = (
        "const {chromium} = require('playwright');\n"
        "(async () => {\n"
        "  const cfg = JSON.parse(process.argv[1]);\n"
        "  try {\n"
        "    const browser = await chromium.launch({headless:true,executablePath:'/usr/bin/chromium',args:['--no-sandbox','--disable-gpu']});\n"
        "    const page = await browser.newPage({viewport:{width:cfg.w,height:cfg.h}});\n"
        "    await page.goto(cfg.url, {waitUntil:'domcontentloaded',timeout:15000});\n"
        "    await new Promise(r=>setTimeout(r,cfg.wait));\n"
        "    const snap = await page.locator('body').ariaSnapshot();\n"
        "    await browser.close();\n"
        "    const txt = snap || 'EMPTY';\n"
        "    if(cfg.out) require('fs').writeFileSync(cfg.out,txt);\n"
        "    process.stdout.write(txt);\n"
        "  } catch(e) {\n"
        "    process.stderr.write(e.message||String(e));\n"
        "    process.exit(1);\n"
        "  }\n"
        "})();\n"
    )
    config = json.dumps({
        "url": url,
        "w": int(viewport_width),
        "h": int(viewport_height),
        "wait": int(wait_ms),
        "out": out_arg,
    })
    try:
        result = subprocess.run(
            ["node", "-e", node_script, config],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(WORKSPACE_ROOT),
        )
        if result.returncode != 0:
            err = (result.stderr or "").strip()[:500]
            return {"success": False, "output": "", "error": f"playwright failed: {err}"}
        tree_text = result.stdout.strip()
        if not tree_text or tree_text == "EMPTY":
            return {"success": False, "output": "", "error": "empty accessibility tree — page may not have rendered"}
        if len(tree_text.encode("utf-8")) > MAX_SCREENSHOT_BYTES:
            tree_text = tree_text[:MAX_SCREENSHOT_BYTES] + "\n[truncated at 2MB]"
        return {
            "success": True,
            "output": tree_text,
            "error": None,
        }
    except FileNotFoundError:
        return {"success": False, "output": "", "error": "node not found — skip visual gate"}
    except subprocess.TimeoutExpired:
        return {"success": False, "output": "", "error": "a11y snapshot timed out after 30s"}
    except Exception as exc:
        return {"success": False, "output": "", "error": str(exc)}


def batch_read(paths: list[str], limit_per_file: int = 200) -> dict:
    """Read multiple files in one call. Returns combined output."""
    results = []
    total_bytes = 0
    MAX_TOTAL = 500_000

    for path in paths:
        if total_bytes >= MAX_TOTAL:
            results.append(f"\n--- {path} ---\n[truncated: total limit reached]")
            break
        try:
            p = resolve_workspace_path(path, WORKSPACE_ROOT)
            if p is None:
                results.append(f"\n--- {path} ---\n[path denied]")
                continue
            if not p.exists():
                results.append(f"\n--- {path} ---\n[not found]")
                continue
            if not p.is_file():
                results.append(f"\n--- {path} ---\n[not a file]")
                continue
            txt = p.read_text(encoding="utf-8", errors="replace")
            lines = txt.splitlines()
            if len(lines) > limit_per_file:
                txt = "\n".join(lines[:limit_per_file]) + f"\n... ({len(lines)} total lines)"
            total_bytes += len(txt.encode("utf-8"))
            results.append(f"\n--- {path} ---\n{txt}")
        except Exception as e:
            results.append(f"\n--- {path} ---\n[error: {e}]")

    combined = "".join(results)
    if len(combined.encode("utf-8")) > MAX_TOTAL:
        combined = combined[:MAX_TOTAL] + "\n[truncated]"

    return {"success": True, "output": combined, "error": None}


def batch_grep(patterns: list[str], path: str = ".", include: str = "", limit_per_pattern: int = 50) -> dict:
    """Search multiple patterns in one call. Returns combined matches."""
    import glob as _glob
    results = []
    total_matches = 0

    for pat in patterns:
        try:
            regex = re.compile(pat, re.IGNORECASE)
        except re.error:
            results.append(f"\n--- pattern: {pat} ---\n[invalid regex]")
            continue

        matches = []
        search_path = resolve_workspace_path(path, WORKSPACE_ROOT)
        if search_path is None:
            results.append(f"\n--- pattern: {pat} ---\n[path denied]")
            continue
        if search_path.is_file():
            file_list = [search_path]
        else:
            pattern = str(search_path / "**" / (include or "*"))
            file_list = [Path(f) for f in _glob.glob(pattern, recursive=True) if Path(f).is_file()]

        for fp in file_list[:200]:
            try:
                txt = fp.read_text(encoding="utf-8", errors="replace")
                for i, line in enumerate(txt.splitlines(), 1):
                    if regex.search(line):
                        matches.append(f"{fp}:{i}: {line.strip()[:200]}")
                        if len(matches) >= limit_per_pattern:
                            break
            except Exception:
                continue
            if len(matches) >= limit_per_pattern:
                break

        total_matches += len(matches)
        results.append(f"\n--- pattern: {pat} ({len(matches)} matches) ---")
        results.extend(matches[:limit_per_pattern])

    combined = "\n".join(results)
    if len(combined.encode("utf-8")) > 200_000:
        combined = combined[:200_000] + "\n[truncated]"

    return {"success": True, "output": combined, "error": None}


# Aliases: bots call these "read" and "write", keep old names for backward compat
file_read = read
file_write = write
