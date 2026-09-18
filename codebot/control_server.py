#!/usr/bin/env python3
"""Botnet control server — stdlib HTTP API for remote Fly.io management.

Purpose
-------
Small ThreadingHTTPServer that exposes botnet control to an authenticated
client. Runs alongside orchestrator.py on Fly.io (same VM, shared
~/Work/bots/state and ~/Work/bots/logs).

Why not reuse orchestrator.py's HTTP? Orchestrator is a pure controller
with no HTTP. This process is intentionally separate — stateless, fast
to restart, and never touches bot prompts. Fly routes external traffic
only here.

Endpoints
---------
GET  /health                    → {"status":"ok", "bots":...}
GET  /bots                      → list bots with model/interval/status/heartbeat age
GET  /bots/{name}               → single bot status + next_run_in/eff_timeout/risk
GET  /bots/{name}/logs?lines=200 → tail of logs/<name>.log
POST /bots/{name}/restart      → restart via orchestrator helper
POST /bots/{name}/pause        → disable (set .drain per-bot)
POST /bots/{name}/resume       → re-enable
POST /bots/start               → body {"bots":["issues","features"]} or {} for all
POST /bots/stop                → body {"bots": [...] } or {} for all
POST /control/drain            → create state/.drain
POST /control/clear-drain      → remove state/.drain
POST /control/update           → run safe_update.sh --force
GET  /state                    → raw orchestrator state dir listing (debug)
GET  /scheduler/status         → versioned redacted ops status (budget, drain,
                                 dead-letter ids, queue/lease counts, paused/disabled,
                                 starvation ages, batch caps, per-model actuals)
GET  /scheduler/events?limit=N → versioned JSONL events, limit clamped to 100
GET  /scheduler/dead-letters   → bounded {id, reason} list (≤20) + count
POST /scheduler/dead-letters/{id}/retry → authenticated idempotent recovery

Auth
----
If CONTROL_TOKEN env is set, require `Authorization: Bearer <token>`.
Fly sets this via `fly secrets set CONTROL_TOKEN=...`.
If CONTROL_TOKEN is unset/empty, the server still rejects all authenticated
endpoints with 401 (fail-closed per Constitution §2).
To test locally, set CONTROL_TOKEN to any value and pass it in requests.

Stdlib-only, single file, no deps beyond orchestrator.py model profiles.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import re
import socket
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)

BOTS_DIR = Path(__file__).resolve().parent
STATE_DIR = BOTS_DIR / "state"
LOGS_DIR = BOTS_DIR / "logs"
ORCH = BOTS_DIR / "orchestrator.py"
SAFE_UPDATE = BOTS_DIR / "safe_update.sh"

CONTROL_TOKEN = os.environ.get("CONTROL_TOKEN", "").strip()
PORT = int(os.environ.get("PORT", os.environ.get("CONTROL_PORT", "8081")))
MAX_LOG_LINES = 2_000
MAX_REQUEST_BYTES = 65_536
REQUEST_TIMEOUT_SECONDS = 15

# Telemetry token from environment; empty means reject all telemetry requests
TELEMETRY_TOKEN = os.environ.get("CODEBOT_TELEMETRY_TOKEN", "").strip()

# Import orchestrator model profiles without starting it
try:
    import importlib.util
    spec = importlib.util.spec_from_file_location("orch_cfg", str(ORCH))
    _m = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(_m)  # type: ignore[union-attr]
    BOT_REGISTRY = _m.BOT_REGISTRY
    MODEL_PROFILES = _m.MODEL_PROFILES
    def eff_timeout(cfg):  # type: ignore[no-untyped-def]
        from dataclasses import dataclass
        # reuse orchestrator effective_heartbeat_timeout logic
        prof = MODEL_PROFILES.get(cfg.model)
        if prof:
            return max(int(cfg.interval_seconds * prof.heartbeat_multiplier), cfg.heartbeat_timeout)
        return cfg.heartbeat_timeout
except Exception:
    # Log import failures so operators detect degraded state instead of silent empty registry
    logger.exception("Failed to load orchestrator module; bot registry will be empty")
    BOT_REGISTRY = []
    MODEL_PROFILES = {}
    def eff_timeout(cfg):  # type: ignore[no-untyped-def]
        return getattr(cfg, "heartbeat_timeout", 600)


def heartbeat_age(name: str) -> float | None:
    p = STATE_DIR / f"{name}.heartbeat"
    try:
        v = float(p.read_text().strip().split()[0])
        return time.time() - v
    except Exception:
        return None


def log_tail(name: str, lines: int = 200) -> str:
    p = LOGS_DIR / f"{name}.log"
    if not p.exists():
        return ""
    try:
        out = subprocess.run(["tail", "-n", str(lines), str(p)], capture_output=True, text=True, timeout=5)
        return out.stdout if out.returncode == 0 else p.read_text()[-8000:]
    except Exception:
        try:
            return p.read_text()[-8000:]
        except Exception:
            return ""


def bot_status(name: str) -> dict:
    cfg = next((c for c in BOT_REGISTRY if c.name == name), None)
    hb = heartbeat_age(name)
    state_file = STATE_DIR / f"{name}.state.json"
    state = {}
    try:
        if state_file.exists():
            state = json.loads(state_file.read_text())
    except Exception:
        pass
    # process check via pgrep
    running = False
    pid = None
    try:
        ps = subprocess.run(["pgrep", "-f", f"api_runner\\.py {name}"], capture_output=True, text=True, timeout=3)
        if ps.stdout.strip():
            running = True
            pid = ps.stdout.strip().split()[0]
    except Exception:
        pass
    # orchestrator process check
    orch_running = False
    try:
        ps2 = subprocess.run(["pgrep", "-f", "orchestrator.py"], capture_output=True, text=True, timeout=3)
        orch_running = bool(ps2.stdout.strip())
    except Exception:
        pass
    eff = eff_timeout(cfg) if cfg else None
    prof = MODEL_PROFILES.get(cfg.model) if cfg else None
    nxt = None
    try:
        nxt = float(state.get("next_run_at", 0)) - time.time() if state.get("next_run_at") else None
        if nxt is not None and nxt < 0:
            nxt = 0
    except Exception:
        nxt = None
    return {
        "name": name,
        "model": cfg.model if cfg else None,
        "interval_seconds": cfg.interval_seconds if cfg else None,
        "effective_timeout": eff,
        "risk": prof.lockup_risk if prof else None,
        "heartbeat_age_seconds": round(hb, 1) if hb is not None else None,
        "running": running,
        "pid": pid,
        "state": state.get("status"),
        "next_run_in_seconds": round(nxt, 1) if nxt is not None else None,
        "restart_count": state.get("restart_count"),
        "orchestrator_running": orch_running,
    }


def scheduler_status() -> dict:
    budget_state = "budget-unknown"
    budget_total = None
    budget_day = None
    per_model: dict[str, dict[str, int]] = {}
    try:
        try:
            from codebot.token_budget import current_day_utc, day_total, get_budget_state
        except ImportError:
            from bots.token_budget import current_day_utc, day_total, get_budget_state
        day = current_day_utc()
        budget_day = day
        ledger_path = STATE_DIR / "token_ledger.json"
        total = day_total(day, path=ledger_path)
        budget_total = int(total) if isinstance(total, (int, float)) else 0
        budget_state = get_budget_state(total)
        try:
            import json as _json

            ledger = _json.loads(ledger_path.read_text(encoding="utf-8")) if ledger_path.exists() else {}
            if isinstance(ledger, dict) and ledger.get("day_utc") == day:
                by_model = ledger.get("by_model")
                if isinstance(by_model, dict):
                    for name, row in list(by_model.items())[:32]:
                        if not isinstance(name, str) or not isinstance(row, dict):
                            continue
                        try:
                            per_model[name] = {
                                "prompt_actual": int(row.get("prompt_actual", 0) or 0),
                                "completion_actual": int(row.get("completion_actual", 0) or 0),
                            }
                        except (TypeError, ValueError):
                            continue
                    bounded_model: dict[str, dict[str, int]] = {}
                    for m_name, m_vals in list(per_model.items())[:32]:
                        if not isinstance(m_name, str) or not isinstance(m_vals, dict):
                            continue
                        if len(m_name) > 64:
                            m_name = m_name[:64]
                        if any(s in m_name.lower() for s in ("token", "secret", "password", "api_key")):
                            continue
                        try:
                            bounded_model[m_name] = {
                                "prompt_actual": max(0, min(int(m_vals.get("prompt_actual", 0) or 0), 10_000_000_000)),
                                "completion_actual": max(0, min(int(m_vals.get("completion_actual", 0) or 0), 10_000_000_000)),
                            }
                        except (TypeError, ValueError):
                            continue
                    per_model = bounded_model
        except (OSError, ValueError):
            per_model = {}
    except (ImportError, OSError, ValueError):
        pass
    dead_letter_count = 0
    dead_letter_ids: list[str] = []
    queue_count = 0
    lease_count = 0
    try:
        try:
            from codebot.lease_state import dead_letters
        except ImportError:
            from bots.lease_state import dead_letters
        letters = dead_letters(STATE_DIR)
        if isinstance(letters, list):
            dead_letter_count = len(letters)
            for letter in letters[:20]:
                if isinstance(letter, dict) and isinstance(letter.get("id"), str):
                    dead_letter_ids.append(letter["id"][:64])
    except (ImportError, OSError, ValueError):
        pass
    try:
        leases_path = STATE_DIR / "leases.json"
        if leases_path.exists():
            try:
                import json as _json2

                raw = _json2.loads(leases_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    leases = raw.get("leases")
                    attempts = raw.get("attempts")
                    if isinstance(leases, dict):
                        lease_count = len(leases)
                    if isinstance(attempts, dict):
                        queue_count = len(attempts)
            except (OSError, ValueError):
                pass
    except (OSError, ValueError):
        pass
    paused_count = 0
    paused_bots: list[str] = []
    disabled_count = 0
    disabled_bots: list[str] = []
    disabled_details: list[dict] = []
    starved: list[dict] = []
    try:
        import json as _json3

        oldest_hb: float | None = None
        per_bot_age: list[tuple[str, float]] = []
        for cfg in BOT_REGISTRY:
            name = cfg.name
            if (STATE_DIR / f"{name}.paused").exists():
                paused_count += 1
                if len(paused_bots) < 20:
                    paused_bots.append(name)
            hb_age = heartbeat_age(name)
            if hb_age is not None:
                per_bot_age.append((name, hb_age))
                if oldest_hb is None or hb_age > oldest_hb:
                    oldest_hb = hb_age
            state_file = STATE_DIR / f"{name}.state.json"
            try:
                if state_file.exists():
                    st = _json3.loads(state_file.read_text(encoding="utf-8"))
                    if isinstance(st, dict) and st.get("status") in ("disabled", "error-disabled"):
                        disabled_count += 1
                        if len(disabled_bots) < 20:
                            disabled_bots.append(name)
                        # Why disabled reason as bounded string: expose operator-visible
                        # status/reason without raw prompts or secrets; truncation enforces caps
                        if len(disabled_details) < 20:
                            raw_status = str(st.get("status", "disabled"))[:64]
                            raw_reason = st.get("reason") or st.get("error") or st.get("disabled_reason") or ""
                            reason_txt = str(raw_reason)[:200] if raw_reason else raw_status
                            try:
                                try:
                                    from codebot.event_log import sanitize_data as _san
                                except ImportError:
                                    from bots.event_log import sanitize_data as _san
                                clean = _san({"reason": reason_txt})
                                reason_txt = str(clean.get("reason", reason_txt))[:256]
                            except Exception:
                                reason_txt = reason_txt[:256]
                            disabled_details.append({"bot": name[:64], "reason": reason_txt})
            except (OSError, ValueError):
                pass
        per_bot_age.sort(key=lambda kv: kv[1], reverse=True)
        for name, age in per_bot_age[:5]:
            try:
                starved.append({"bot": name, "starvation_age_seconds": round(float(age), 1)})
            except (TypeError, ValueError):
                continue
    except (OSError, ValueError):
        pass
    batch_utilization: dict[str, int] = {"max_per_batch": 5, "max_batches": 2, "stagger_s": 20}
    return {
        "version": 1,
        "budget_state": budget_state,
        "budget_day": budget_day,
        "budget_total_actual": budget_total,
        "per_model_actual": per_model,
        "drain": (STATE_DIR / ".drain").exists(),
        "dead_letter_count": dead_letter_count,
        "dead_letter_ids": dead_letter_ids,
        "queue_tracked": queue_count,
        "lease_active": lease_count,
        "paused_count": paused_count,
        "paused_bots": paused_bots,
        "disabled_count": disabled_count,
        "disabled_bots": disabled_bots,
        "disabled_details": disabled_details,
        "starved_oldest": starved[0] if starved else None,
        "starved_top": starved,
        "batch_utilization": batch_utilization,
    }


def retry_dead_letter(item_id: str) -> dict:
    try:
        try:
            from codebot.lease_state import retry_dead_letter as retry
        except ImportError:
            from bots.lease_state import retry_dead_letter as retry
        result = retry(STATE_DIR, item_id)
        if result["status"] == "retried":
            try:
                try:
                    from codebot.event_log import append_event
                except ImportError:
                    from bots.event_log import append_event
                append_event(STATE_DIR, "dead-letter-retry", {"id": item_id})
            except (ImportError, OSError, ValueError):
                pass
        return result
    except (ImportError, OSError, ValueError):
        return {"status": "unavailable", "id": item_id}


class Handler(BaseHTTPRequestHandler):
    def _auth(self) -> bool:
        """Validate Bearer token via Authorization header.

        Fail-closed: when CONTROL_TOKEN is unset/empty, reject all requests
        (Constitution §2: no implicit trust at auth boundaries).
        """
        if not CONTROL_TOKEN:
            logger.warning(
                "CONTROL_TOKEN is not set — all authenticated requests are rejected "
                "(set CONTROL_TOKEN env var to enable API access)"
            )
            return False
        auth = self.headers.get("Authorization", "")
        expected = f"Bearer {CONTROL_TOKEN}"
        return hmac.compare_digest(auth.strip(), expected)

    def _json(self, code: int, obj: dict | list) -> None:
        """Send JSON response with security headers (Constitution §2)."""
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        # Security headers per Constitution §2
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> tuple[dict | None, int | None, str | None]:
        raw_length = self.headers.get("Content-Length")
        if not raw_length:
            return {}, None, None
        try:
            length = int(raw_length)
        except ValueError:
            return None, 400, "Content-Length must be an integer"
        if length < 0:
            return None, 400, "Content-Length must not be negative"
        if length > MAX_REQUEST_BYTES:
            return None, 413, "request body too large"
        try:
            self.connection.settimeout(REQUEST_TIMEOUT_SECONDS)
            raw = self.rfile.read(length)
        except (OSError, TimeoutError, socket.timeout):
            return None, 408, "request body read timed out"
        try:
            body = json.loads(raw.decode()) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None, 400, "request body must be valid JSON"
        if not isinstance(body, dict):
            return None, 400, "request body must be a JSON object"
        return body, None, None

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        # /health is public — Fly's http_service.checks GETs /health without Bearer.
        # Keep it cheap: no auth, no bot scan, just ok + timestamp.
        if path in ("/health", "/api/health"):
            self._json(200, {"status": "ok", "time": time.time()})
            return

        if not self._auth():
            self._json(401, {"error": "unauthorized"})
            return

        if path in ("/bots", "/api/bots"):
            self._json(200, [bot_status(c.name) for c in BOT_REGISTRY])
            return

        if path in ("/scheduler/status", "/api/scheduler/status"):
            self._json(200, scheduler_status())
            return

        if path in ("/scheduler/events", "/api/scheduler/events"):
            try:
                limit_raw = qs.get("limit", ["100"])[0]
            except (IndexError, AttributeError):
                limit_raw = "100"
            try:
                limit = int(limit_raw)
            except (TypeError, ValueError):
                self._json(400, {"error": "limit must be an integer"})
                return
            try:
                event_type = qs.get("type", [None])[0]
            except (IndexError, AttributeError):
                event_type = None
            if event_type is not None and not isinstance(event_type, str):
                event_type = str(event_type)
            if event_type is not None and len(event_type) > 64:
                event_type = event_type[:64]
            try:
                try:
                    from codebot.event_log import MAX_EVENTS as _MAX_EVENTS, read_events, sanitize_data
                except ImportError:
                    from bots.event_log import MAX_EVENTS as _MAX_EVENTS, read_events, sanitize_data
                bounded = max(1, min(limit, _MAX_EVENTS))
                if event_type:
                    records = read_events(STATE_DIR, limit=_MAX_EVENTS)
                    filtered = [r for r in records if isinstance(r, dict) and r.get("type") == event_type]
                    records = filtered[-bounded:] if len(filtered) > bounded else filtered
                else:
                    records = read_events(STATE_DIR, limit=bounded)[:bounded]
                safe_events = []
                for record in records:
                    if not isinstance(record, dict):
                        continue
                    data = record.get("data")
                    safe_events.append(
                        {
                            "version": 1,
                            "type": record.get("type"),
                            "ts": record.get("ts"),
                            "data": sanitize_data(data if isinstance(data, dict) else {}),
                        }
                    )
                payload = {"version": 1, "limit": bounded, "events": safe_events}
                if event_type:
                    payload["type"] = event_type[:64]
                self._json(200, payload)
            except (ImportError, OSError, ValueError):
                self._json(200, {"version": 1, "limit": 0, "events": []})
            return

        if path in ("/scheduler/dead-letters", "/api/scheduler/dead-letters"):
            try:
                try:
                    from codebot.lease_state import dead_letters
                except ImportError:
                    from bots.lease_state import dead_letters
                letters = dead_letters(STATE_DIR)
                safe = [
                    {"id": str(item.get("id", ""))[:64], "reason": str(item.get("reason", ""))[:256]}
                    for item in letters[:20]
                    if isinstance(item, dict)
                ]
                self._json(200, {"version": 1, "count": len(letters) if isinstance(letters, list) else 0, "dead_letters": safe})
            except (ImportError, OSError, ValueError):
                self._json(200, {"version": 1, "count": 0, "dead_letters": []})
            return

        m = re.match(r"^/(?:api/)?bots/([^/]+)/logs$", path)
        if m:
            name = m.group(1)
            try:
                lines = int(qs.get("lines", ["200"])[0])
            except ValueError:
                self._json(400, {"error": "lines must be an integer"})
                return
            lines = max(1, min(lines, MAX_LOG_LINES))
            if not any(c.name == name for c in BOT_REGISTRY):
                self._json(404, {"error": "unknown bot"})
                return
            self._json(200, {"name": name, "lines": lines, "tail": log_tail(name, lines)})
            return

        m = re.match(r"^/(?:api/)?bots/([^/]+)$", path)
        if m:
            name = m.group(1)
            if not any(c.name == name for c in BOT_REGISTRY):
                self._json(404, {"error": "unknown bot"})
                return
            self._json(200, bot_status(name))
            return

        # Delegate telemetry health check to TelemetryHandler logic
        if path in ("/telemetry/health", "/api/telemetry/health"):
            self._json(200, {"status": "ok", "time": time.time()})
            return

        if path in ("/state", "/api/state"):
            try:
                files = []
                for p in sorted(STATE_DIR.glob("*")):
                    try:
                        st = p.stat()
                        files.append({"name": p.name, "size": st.st_size, "mtime": st.st_mtime})
                    except Exception:
                        pass
                self._json(200, files)
            except Exception as e:
                self._json(500, {"error": str(e)})
            return

        self._json(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        if not self._auth():
            self._json(401, {"error": "unauthorized"})
            return
        parsed = urlparse(self.path)
        path = parsed.path
        body, error_code, error = self._read_json_body()
        if error_code is not None:
            self._json(error_code, {"error": error})
            return

        # POST /bots/{name}/restart
        m = re.match(r"^/(?:api/)?bots/([^/]+)/restart$", path)
        if m:
            name = m.group(1)
            if not any(c.name == name for c in BOT_REGISTRY):
                self._json(404, {"error": "unknown bot"})
                return
            # ask orchestrator via pkill + let interval-aware respawn handle, or direct start
            try:
                # kill existing if running
                subprocess.run(["pkill", "-f", f"api_runner\\.py {name}"], timeout=5)
                time.sleep(1)
                # orchestrator will respawn on next health check if waiting; force start via orchestrator CLI
                subprocess.Popen(["python3", str(ORCH), "--start", name], cwd=str(BOTS_DIR))
                self._json(200, {"ok": True, "action": "restart", "bot": name})
            except Exception as e:
                self._json(500, {"error": str(e)})
            return

        m = re.match(r"^/(?:api/)?bots/([^/]+)/pause$", path)
        if m:
            name = m.group(1)
            try:
                (STATE_DIR / f"{name}.paused").write_text(str(time.time()))
                subprocess.run(["pkill", "-f", f"api_runner\\.py {name}"], timeout=5)
                self._json(200, {"ok": True, "paused": name})
            except Exception as e:
                self._json(500, {"error": str(e)})
            return

        m = re.match(r"^/(?:api/)?bots/([^/]+)/resume$", path)
        if m:
            name = m.group(1)
            try:
                p = STATE_DIR / f"{name}.paused"
                if p.exists():
                    p.unlink()
                subprocess.Popen(["python3", str(ORCH), "--start", name], cwd=str(BOTS_DIR))
                self._json(200, {"ok": True, "resumed": name})
            except Exception as e:
                self._json(500, {"error": str(e)})
            return

        if path in ("/bots/start", "/api/bots/start"):
            bots = body.get("bots") or [c.name for c in BOT_REGISTRY]
            try:
                subprocess.Popen(["python3", str(ORCH), "--start", *bots], cwd=str(BOTS_DIR))
                self._json(200, {"ok": True, "started": bots})
            except Exception as e:
                self._json(500, {"error": str(e)})
            return

        if path in ("/bots/stop", "/api/bots/stop", "/control/stop"):
            bots = body.get("bots")
            try:
                if bots:
                    for n in bots:
                        subprocess.run(["pkill", "-f", f"api_runner\\.py {n}"], timeout=5)
                else:
                    subprocess.run(["pkill", "-f", "orchestrator.py"], timeout=5)
                    subprocess.run(["pkill", "-f", "[a]pi_runner\\.py"], timeout=5)
                self._json(200, {"ok": True, "stopped": bots or "all"})
            except Exception as e:
                self._json(500, {"error": str(e)})
            return

        if path in ("/control/drain", "/api/control/drain"):
            try:
                (STATE_DIR / ".drain").write_text(str(time.time()))
                self._json(200, {"ok": True, "drain": True})
            except Exception as e:
                self._json(500, {"error": str(e)})
            return

        if path in ("/control/clear-drain", "/api/control/clear-drain"):
            try:
                p = STATE_DIR / ".drain"
                if p.exists():
                    p.unlink()
                self._json(200, {"ok": True, "drain": False})
            except Exception as e:
                self._json(500, {"error": str(e)})
            return

        if path in ("/control/update", "/api/control/update"):
            try:
                proc = subprocess.run([str(SAFE_UPDATE), "--force"], cwd=str(BOTS_DIR), capture_output=True, text=True, timeout=120)
                self._json(200, {"ok": proc.returncode == 0, "returncode": proc.returncode, "stdout": proc.stdout[-4000:], "stderr": proc.stderr[-4000:]})
            except Exception as e:
                self._json(500, {"error": str(e)})
            return

        # POST /telemetry — production telemetry ingestion
        if path in ("/telemetry", "/api/telemetry"):
            self._handle_telemetry(body)
            return

        m = re.match(r"^/(?:api/)?scheduler/dead-letters/(Q-\d+)/retry$", path)
        if m:
            result = retry_dead_letter(m.group(1))
            self._json(200, result)
            return

        self._json(404, {"error": "not found"})

    def _handle_telemetry(self, body: dict) -> None:
        """Handle POST /telemetry — ingest production telemetry signals.

        Validates the signal, creates a ticket candidate in DISCOVERED state,
        stores the event for trend analysis, and triggers anomaly detection.
        """
        # Check telemetry-specific auth
        if TELEMETRY_TOKEN:
            auth = self.headers.get("Authorization", "")
            if auth.strip() != f"Bearer {TELEMETRY_TOKEN}":
                self._json(401, {"error": "unauthorized"})
                return
        elif not CONTROL_TOKEN:
            # If neither token is set, reject telemetry (safer default)
            self._json(401, {"error": "telemetry token not configured"})
            return

        # Import telemetry validation and ticket creation
        try:
            from codebot.telemetry import (
                _validate_signal,
                _create_ticket_from_signal,
                detect_anomalies,
            )
            from codebot.event_log import append_event
        except ImportError as e:
            logger.error("Failed to import telemetry modules: %s", e)
            self._json(500, {"error": "telemetry subsystem unavailable"})
            return

        # Validate input at trust boundary
        is_valid, error_msg = _validate_signal(body)
        if not is_valid:
            self._json(400, {"error": error_msg})
            return

        # Store telemetry event for trend analysis
        try:
            append_event(STATE_DIR, "telemetry", body)
        except Exception as e:
            logger.warning("Failed to append telemetry event: %s", e)

        # Create ticket candidate
        result = _create_ticket_from_signal(body, STATE_DIR)

        if result.get("success"):
            response = {
                "ok": True,
                "ticket_id": result.get("ticket_id"),
                "message": "signal accepted, ticket candidate created (requires human triage)",
            }
            if result.get("duplicate"):
                response["message"] = "signal already tracked (duplicate)"

            # Anomaly detection: read recent telemetry events and check for spikes
            try:
                from codebot.event_log import read_events
                recent_events = read_events(STATE_DIR, limit=50)
                telemetry_signals = [
                    e.get("data", {}) for e in recent_events
                    if e.get("type") == "telemetry"
                ]
                # Baseline: assume 2 errors per window is normal
                baseline_rate = 2.0
                anomalies = detect_anomalies(telemetry_signals, baseline_rate)
                if anomalies:
                    for anomaly in anomalies:
                        append_event(STATE_DIR, "discovery-trigger", anomaly)
                    response["anomalies_detected"] = len(anomalies)
                    logger.info(
                        "Telemetry anomaly detected: %d anomalies, discovery triggered",
                        len(anomalies),
                    )
            except Exception as e:
                logger.warning("Anomaly detection failed: %s", e)

            self._json(201, response)
        else:
            self._json(500, {"error": result.get("error", "internal error")})

    def log_message(self, format, *args):  # noqa: A002
        # quiet except errors; fly logs capture stdout
        pass


def main() -> None:
    addr = ("0.0.0.0", PORT)
    print(f"control_server listening on {addr[0]}:{addr[1]}  bots={len(BOT_REGISTRY)}  drain={(STATE_DIR / '.drain').exists()}", flush=True)
    httpd = ThreadingHTTPServer(addr, Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
