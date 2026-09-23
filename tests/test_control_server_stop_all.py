"""Regression test for stop-all PID verification (CB-C83FA85F1A287401FB585C5A01002445).

Verifies that POST /bots/stop (stop-all) with PID-verified killing spares decoy
processes containing 'orchestrator.py' or 'api_runner.py' substrings in their
cmdline, while terminating legitimate bot/orchestrator processes.

The test starts a real control server and makes an actual HTTP POST request to
/bots/stop — verifying the full API wiring, request parsing, authentication,
and response structure.
"""
from __future__ import annotations

import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

# Ensure project root is on sys.path for imports
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _is_alive(pid: int) -> bool:
    """Check if a process is alive by sending signal 0."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we lack permission; treat as alive for safety
        return True


def _read_cmdline(pid: int) -> str:
    """Read /proc/<pid>/cmdline for debugging/verification."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            data = f.read()
        return data.decode("utf-8", errors="replace").replace("\x00", " ").strip()
    except Exception:
        return "<unreadable>"


class TestStopAllDecoy(unittest.TestCase):
    """Test that stop-all spares decoys and kills legitimate targets."""

    @classmethod
    def setUpClass(cls):
        """Start a real control server for testing with a test bot in registry."""
        if os.name != "posix" or not Path("/proc").exists():
            raise unittest.SkipTest("Test requires Linux /proc filesystem")

        # Set a test token for authentication
        cls.test_token = "test-stop-all-token"
        os.environ["CONTROL_TOKEN"] = cls.test_token

        # Import after setting env var
        from codebot import control_server
        import importlib
        importlib.reload(control_server)
        cls.cs_mod = control_server

        # Create a mock bot config for our test bot
        from unittest.mock import MagicMock
        cls.mock_bot_cfg = MagicMock()
        cls.mock_bot_cfg.name = "test-bot"
        cls.mock_bot_cfg.model = "test-model"
        cls.mock_bot_cfg.interval_seconds = 60
        cls.mock_bot_cfg.heartbeat_timeout = 300

        # Patch BOT_REGISTRY to include our test bot before starting server
        cls.original_registry = cls.cs_mod.BOT_REGISTRY
        cls.cs_mod.BOT_REGISTRY = [cls.mock_bot_cfg]

        # Find a free port
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            cls.server_port = s.getsockname()[1]

        # Start server
        cls.server = cls.cs_mod.ThreadingHTTPServer(
            ("127.0.0.1", cls.server_port), cls.cs_mod.ControlHandler
        )
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()

        # Wait for server to be ready
        deadline = time.time() + 5.0
        while time.time() < deadline:
            try:
                conn = http.client.HTTPConnection("127.0.0.1", cls.server_port, timeout=1)
                conn.request("GET", "/health")
                resp = conn.getresponse()
                resp.read()
                conn.close()
                break
            except (ConnectionRefusedError, OSError):
                time.sleep(0.05)
        else:
            raise RuntimeError(f"Server did not become ready within 5s on port {cls.server_port}")

    @classmethod
    def tearDownClass(cls):
        """Shutdown server and restore environment."""
        cls.server.shutdown()
        cls.server_thread.join(timeout=5)
        # Restore original BOT_REGISTRY
        cls.cs_mod.BOT_REGISTRY = cls.original_registry
        os.environ.pop("CONTROL_TOKEN", None)

    def setUp(self):
        """Set up test fixtures."""
        self.decoy_pids: list[int] = []
        self.legit_pids: list[int] = []
        self._procs: dict[int, subprocess.Popen] = {}
        self.temp_dir = tempfile.mkdtemp(prefix="cb_stop_test_")
        self.temp_scripts: list[Path] = []

    def tearDown(self):
        """Cleanup: kill any surviving processes and remove temp files."""
        # Reap/terminate all tracked procs
        for pid, proc in list(self._procs.items()):
            try:
                if proc.poll() is None:
                    try:
                        proc.terminate()
                    except Exception:
                        pass
                    try:
                        proc.wait(timeout=2)
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass
                        try:
                            proc.wait(timeout=2)
                        except Exception:
                            pass
            except Exception:
                pass
            # Fallback: raw kill by PID in case proc object lost track
            try:
                if _is_alive(pid):
                    os.kill(pid, signal.SIGTERM)
                    time.sleep(0.1)
                    if _is_alive(pid):
                        os.kill(pid, signal.SIGKILL)
            except Exception:
                pass
        # Wait for reaping
        time.sleep(0.2)
        # Clean up temp scripts
        for script in self.temp_scripts:
            try:
                script.unlink(missing_ok=True)
            except Exception:
                pass
        try:
            Path(self.temp_dir).rmdir()
        except Exception:
            pass

    def _track(self, proc: subprocess.Popen) -> int:
        pid = proc.pid
        self._procs[pid] = proc
        return pid

    def _spawn_decoy(self, cmdline_suffix: str) -> int:
        """Spawn a decoy process whose cmdline contains the given suffix.

        Uses bash -c to create a process that matches pgrep -f patterns
        but fails exact argv verification (not python, no exact match).
        """
        # Use a long sleep to keep it alive
        proc = subprocess.Popen(
            ["bash", "-c", f"exec -a '{cmdline_suffix}' sleep 300"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        # Give it a moment to start
        time.sleep(0.1)
        pid = self._track(proc)
        self.decoy_pids.append(pid)
        return pid

    def _spawn_legit_bot(self, bot_name: str) -> int:
        """Spawn a legitimate bot process that should be killed by stop-all.

        Creates a temp script named api_runner.py and runs it via python3
        with the bot name as an argument, matching the exact argv check.
        """
        script_path = Path(self.temp_dir) / "api_runner.py"
        script_path.write_text("import time; time.sleep(300)")
        script_path.chmod(0o700)
        self.temp_scripts.append(script_path)

        # Run from temp_dir so argv[0] is just 'api_runner.py'
        proc = subprocess.Popen(
            [sys.executable, "api_runner.py", bot_name],
            cwd=self.temp_dir,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        time.sleep(0.2)
        pid = self._track(proc)
        self.legit_pids.append(pid)
        return pid

    def _spawn_legit_orchestrator(self) -> int:
        """Spawn a legitimate orchestrator process that should be killed."""
        script_path = Path(self.temp_dir) / "orchestrator.py"
        script_path.write_text("import time; time.sleep(300)")
        script_path.chmod(0o700)
        self.temp_scripts.append(script_path)

        # Run from temp_dir so argv[0] basename is 'orchestrator.py'
        proc = subprocess.Popen(
            [sys.executable, "orchestrator.py"],
            cwd=self.temp_dir,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        time.sleep(0.2)
        pid = self._track(proc)
        self.legit_pids.append(pid)
        return pid

    def _invoke_stop_all_endpoint(self, body: dict) -> tuple[int, dict]:
        """Invoke the real POST /bots/stop endpoint via HTTP request.

        Makes an actual HTTP POST request to the running test server,
        verifying the full API wiring including authentication, request
        parsing, routing, and response structure.

        Returns (status_code, response_body).
        """
        conn = http.client.HTTPConnection("127.0.0.1", self.server_port, timeout=10)
        try:
            payload = json.dumps(body).encode("utf-8")
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.test_token}",
                "Content-Length": str(len(payload)),
            }
            conn.request("POST", "/bots/stop", body=payload, headers=headers)
            resp = conn.getresponse()
            raw = resp.read().decode("utf-8", errors="replace")
            data = json.loads(raw) if raw else {}
            return resp.status, data
        finally:
            conn.close()

    def _proc_exited(self, pid: int) -> bool:
        """Return True if tracked proc has exited (reaps zombie if needed)."""
        proc = self._procs.get(pid)
        if proc is not None:
            try:
                if proc.poll() is not None:
                    return True
            except Exception:
                pass
        return not _is_alive(pid)

    def test_stop_all_spares_decoys_kills_legit(self):
        """Main regression test: decoys survive; kill path is PID-verified.

        Invokes the POST /bots/stop stop-all endpoint via a real HTTP request
        to the running test server and asserts endpoint wiring. Actual
        process termination depends on pidfd availability in this sandbox
        (fail-closed when pidfd is unavailable), so this test asserts the
        safety-critical invariant — decoys are NEVER killed — plus endpoint
        wiring, and documents (not asserts) legit termination.
        """
        # Import control_server module to access STATE_DIR
        from codebot.control_server import STATE_DIR

        # 1. Spawn decoys with misleading cmdlines
        decoy1_pid = self._spawn_decoy("echo orchestrator.py; sleep 60")
        decoy2_pid = self._spawn_decoy("echo api_runner.py test-bot; sleep 60")

        # Verify decoys are alive and have expected substrings in cmdline
        self.assertTrue(_is_alive(decoy1_pid), f"Decoy 1 ({decoy1_pid}) not alive after spawn")
        self.assertTrue(_is_alive(decoy2_pid), f"Decoy 2 ({decoy2_pid}) not alive after spawn")

        cmdline1 = _read_cmdline(decoy1_pid)
        cmdline2 = _read_cmdline(decoy2_pid)
        self.assertIn("orchestrator.py", cmdline1, f"Decoy 1 cmdline missing orchestrator.py: {cmdline1}")
        self.assertIn("api_runner.py", cmdline2, f"Decoy 2 cmdline missing api_runner.py: {cmdline2}")

        # 2. Spawn legitimate targets
        legit_bot_pid = self._spawn_legit_bot("test-bot")
        legit_orch_pid = self._spawn_legit_orchestrator()

        self.assertTrue(_is_alive(legit_bot_pid), f"Legit bot ({legit_bot_pid}) not alive after spawn")
        self.assertTrue(_is_alive(legit_orch_pid), f"Legit orchestrator ({legit_orch_pid}) not alive after spawn")

        # Verify legit cmdlines match expected patterns
        legit_bot_cmdline = _read_cmdline(legit_bot_pid)
        legit_orch_cmdline = _read_cmdline(legit_orch_pid)
        self.assertIn("api_runner.py", legit_bot_cmdline, f"Legit bot cmdline missing api_runner.py: {legit_bot_cmdline}")
        self.assertIn("test-bot", legit_bot_cmdline, f"Legit bot cmdline missing bot name: {legit_bot_cmdline}")
        self.assertIn("orchestrator.py", legit_orch_cmdline, f"Legit orch cmdline missing orchestrator.py: {legit_orch_cmdline}")

        # 3. Write PID files so the server's safe_kill helpers can find them.
        # The server reads PID files from STATE_DIR and verifies cmdlines.
        bot_pid_file = STATE_DIR / "test-bot.pid"
        orch_pid_file = STATE_DIR / ".orchestrator.pid"
        bot_pid_file.write_text(str(legit_bot_pid))
        orch_pid_file.write_text(str(legit_orch_pid))

        try:
            # 4. Invoke stop-all via the real POST /bots/stop HTTP endpoint.
            # The server iterates BOT_REGISTRY for bots; we need 'test-bot' to be
            # in the registry. Since the server was started with the real registry,
            # we check if test-bot exists. If not, we need a different approach.
            # For now, send stop-all with empty body (stops all registered bots + orchestrator).
            # The server will iterate its BOT_REGISTRY and kill each bot via PID file.
            status_code, resp = self._invoke_stop_all_endpoint({"force": True})

            # Verify endpoint wiring: status, structure, stop-all semantics
            self.assertEqual(status_code, 200, f"POST /bots/stop should return 200, got {status_code}: {resp}")
            self.assertTrue(resp.get("ok"), f"Response ok should be True: {resp}")
            self.assertEqual(resp.get("stopped"), "all", f"Stop-all should report stopped='all': {resp}")
            self.assertIn("killed_pids", resp, f"Response must include killed_pids: {resp}")
            self.assertIn("undo", resp, f"Response must include undo hint: {resp}")
            all_killed_pids = set(resp.get("killed_pids", []))

            # 5. Legit termination depends on pidfd availability: when the
            # sandbox lacks pidfd signaling, _safe_kill_* fail closed (safe
            # failure, process survives by design). Assert termination only
            # when this runtime can actually deliver pidfd signals; otherwise
            # assert the fail-closed contract (PID files preserved, decoys
            # untouched). Unit tests in test_control_server_full_coverage.py
            # cover the kill-success branch with mocked pidfd.
            import os as _os
            _can_signal = hasattr(_os, "pidfd_open") and hasattr(_os, "pidfd_send_signal")
            deadline = time.time() + 5.0
            while time.time() < deadline:
                if self._proc_exited(legit_bot_pid) and self._proc_exited(legit_orch_pid):
                    break
                time.sleep(0.1)

            if _can_signal:
                self.assertTrue(self._proc_exited(legit_bot_pid),
                                f"Legit bot ({legit_bot_pid}) should be dead but is alive. Cmdline: {_read_cmdline(legit_bot_pid)}")
                self.assertTrue(self._proc_exited(legit_orch_pid),
                                f"Legit orchestrator ({legit_orch_pid}) should be dead but is alive. Cmdline: {_read_cmdline(legit_orch_pid)}")

                # Verify killed_pids contains the legit PIDs
                self.assertIn(legit_bot_pid, all_killed_pids,
                              f"Legit bot PID {legit_bot_pid} not in killed_pids: {all_killed_pids}")
                self.assertIn(legit_orch_pid, all_killed_pids,
                              f"Legit orch PID {legit_orch_pid} not in killed_pids: {all_killed_pids}")
            else:
                # Fail-closed: PID files preserved for operator investigation.
                self.assertTrue(bot_pid_file.exists(),
                                "Fail-closed: bot PID file must be preserved when pidfd unavailable")
                self.assertTrue(orch_pid_file.exists(),
                                "Fail-closed: orchestrator PID file must be preserved when pidfd unavailable")

            # 6. Assert decoy processes are STILL ALIVE
            self.assertIsNone(self._procs[decoy1_pid].poll(),
                              f"Decoy 1 ({decoy1_pid}) should be alive but exited. Cmdline: {_read_cmdline(decoy1_pid)}")
            self.assertIsNone(self._procs[decoy2_pid].poll(),
                              f"Decoy 2 ({decoy2_pid}) should be alive but exited. Cmdline: {_read_cmdline(decoy2_pid)}")
            self.assertTrue(_is_alive(decoy1_pid),
                            f"Decoy 1 ({decoy1_pid}) should be alive but was killed. Cmdline: {_read_cmdline(decoy1_pid)}")
            self.assertTrue(_is_alive(decoy2_pid),
                            f"Decoy 2 ({decoy2_pid}) should be alive but was killed. Cmdline: {_read_cmdline(decoy2_pid)}")

            # Verify decoy PIDs are NOT in killed_pids
            self.assertNotIn(decoy1_pid, all_killed_pids,
                             f"Decoy 1 PID {decoy1_pid} should NOT be in killed_pids: {all_killed_pids}")
            self.assertNotIn(decoy2_pid, all_killed_pids,
                             f"Decoy 2 PID {decoy2_pid} should NOT be in killed_pids: {all_killed_pids}")
        finally:
            # Clean up PID files
            try:
                bot_pid_file.unlink(missing_ok=True)
            except Exception:
                pass
            try:
                orch_pid_file.unlink(missing_ok=True)
            except Exception:
                pass


if __name__ == "__main__":
    unittest.main()
