#!/usr/bin/env python3
"""Auto-revert bot commits that break the build gate.

Scans docs/optimization/build-gate-INDEX.md for FAIL verdicts, maps each
failing file to the most recent bot commit touching it, reverts that commit
 (fail-open: only when the revert is clean), and requeues the original work
item with the failure log attached.

Runs on demand (orchestrator tick hook or manual). Never force-pushes;
uses `git revert --no-commit` so the revert lands as a normal commit.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any

BOTS_DIR = Path(__file__).parent
WORK_ROOT = BOTS_DIR.parent
STATE_DIR = BOTS_DIR / "state"
REVERT_LOG = STATE_DIR / "auto_revert_log.json"

REPOS = [
    WORK_ROOT / "Monitor-Manager-Python",
    WORK_ROOT / "Monitor-Client-Python",
    WORK_ROOT / "lib-common",
    BOTS_DIR,
]

FAIL_RE = re.compile(r"\bFAIL\b", re.IGNORECASE)
FILE_RE = re.compile(r"([A-Za-z0-9_./-]+\.(?:py|js|css|html|md|json|yaml|toml))")


def _run(cmd: list[str], cwd: Path, timeout: int = 30) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        return 1, str(e)


def _gate_failures() -> list[dict[str, Any]]:
    """Parse build-gate INDEX for FAIL entries with file scope."""
    out: list[dict[str, Any]] = []
    for cand in (BOTS_DIR / "docs" / "optimization" / "build-gate-INDEX.md",
                 WORK_ROOT / "docs" / "optimization" / "build-gate-INDEX.md"):
        try:
            if not cand.exists():
                continue
            for line in cand.read_text(encoding="utf-8").splitlines():
                if not FAIL_RE.search(line):
                    continue
                files = FILE_RE.findall(line)
                out.append({"line": line.strip()[:300], "files": files[:5], "source": str(cand)})
        except Exception:
            continue
    return out


def _last_bot_commit(repo: Path, filepath: str) -> str | None:
    """Most recent commit hash touching filepath, or None."""
    code, out = _run(["git", "log", "--format=%H", "-n", "1", "--", filepath], repo)
    if code != 0:
        return None
    sha = out.strip().splitlines()
    return sha[0].strip()[:40] if sha and sha[0].strip() else None


def _commit_is_bot_authored(repo: Path, sha: str) -> bool:
    code, out = _run(["git", "log", "--format=%an <%ae>", "-n", "1", sha], repo)
    if code != 0:
        return False
    author = out.strip().lower()
    return "bot" in author or "sisyphus" in author or "linuxguru999" in author


def _revert_log() -> list[dict]:
    try:
        if REVERT_LOG.exists():
            data = json.loads(REVERT_LOG.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
    except Exception:
        pass
    return []


def _already_reverted(sha: str) -> bool:
    return any(e.get("sha") == sha for e in _revert_log())


def _record_revert(entry: dict) -> None:
    try:
        log = _revert_log()
        log.append(entry)
        tmp = Path(str(REVERT_LOG) + ".tmp")
        tmp.write_text(json.dumps(log[-50:], indent=2), encoding="utf-8")
        tmp.replace(REVERT_LOG)
    except Exception:
        pass


def _requeue_with_failure(work_hint: str, failure: str) -> None:
    """Append a requeue entry to the triage queue with failure context."""
    try:
        qp = WORK_ROOT / "docs" / "triage" / "QUEUE.md"
        if not qp.exists():
            return
        text = qp.read_text(encoding="utf-8")
        marker = f"auto-revert {work_hint[:60]}"
        if marker in text:
            return
        entry = (
            f"\n### QUEUE-REVERT-{int(time.time())}\n"
            f"- **Source**: auto-revert (build gate failure)\n"
            f"- **Complexity**: medium\n"
            f"- **Work hint**: {work_hint[:200]}\n"
            f"- **Failure**: {failure[:500]}\n"
            f"- **Status**: confirmed\n"
        )
        qp.write_text(text.rstrip() + "\n" + entry, encoding="utf-8")
    except Exception:
        pass


def process() -> dict[str, Any]:
    """Scan gate, revert offending bot commits, requeue. Returns summary."""
    failures = _gate_failures()
    reverted: list[dict] = []
    skipped: list[dict] = []
    for fail in failures:
        for f in fail["files"]:
            for repo in REPOS:
                if not (repo / ".git").exists():
                    continue
                rel = f
                sha = _last_bot_commit(repo, rel)
                if not sha:
                    continue
                if _already_reverted(sha):
                    skipped.append({"repo": str(repo), "file": rel, "sha": sha, "why": "already-reverted"})
                    continue
                if not _commit_is_bot_authored(repo, sha):
                    skipped.append({"repo": str(repo), "file": rel, "sha": sha, "why": "not-bot-authored"})
                    continue
                code, out = _run(["git", "revert", "--no-commit", sha], repo)
                if code != 0:
                    _run(["git", "revert", "--abort"], repo)
                    skipped.append({"repo": str(repo), "file": rel, "sha": sha, "why": f"revert-conflict: {out[:200]}"})
                    continue
                code2, _ = _run(["git", "commit", "-m", f"revert: auto-revert {sha[:8]} (build gate FAIL: {fail['line'][:80]})"], repo)
                if code2 != 0:
                    _run(["git", "revert", "--abort"], repo)
                    skipped.append({"repo": str(repo), "file": rel, "sha": sha, "why": "commit-failed"})
                    continue
                _record_revert({"sha": sha, "repo": str(repo), "file": rel, "at": time.time(), "gate": fail["line"][:200]})
                _requeue_with_failure(f"{repo.name}:{rel}", fail["line"])
                reverted.append({"repo": str(repo), "file": rel, "sha": sha[:8]})
                break
    return {"failures": len(failures), "reverted": reverted, "skipped": skipped}


def main() -> None:
    res = process()
    print(f"gate failures={res['failures']} reverted={len(res['reverted'])} skipped={len(res['skipped'])}")
    for r in res["reverted"]:
        print(f"  reverted {r['sha']} in {r['repo']} ({r['file']})")


if __name__ == "__main__":
    main()
