"""Tests for gatekeeper.py."""
import json
import pytest
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from codebot.gatekeeper import Gatekeeper, MAX_REWORK_ATTEMPTS

def _policy(tmp_path, cmd):
    p = tmp_path / "gates.yaml"
    p.write_text(f"required:\n  - name: t\n    command: {cmd}\n")
    return p

class TestGatekeeperVerify:
    def test_pass_yields_complete(self, tmp_path):
        ws = tmp_path / "ws"; ws.mkdir()
        gk = Gatekeeper(tmp_path / "state", policy_path=_policy(tmp_path, "echo ok"), workspace=ws)
        r = gk.verify_ticket("CB-1", "bug", ["f.py"])
        assert r["decision"] == "COMPLETE"

    def test_fail_yields_rework(self, tmp_path):
        ws = tmp_path / "ws"; ws.mkdir()
        gk = Gatekeeper(tmp_path / "state", policy_path=_policy(tmp_path, "false"), workspace=ws)
        r = gk.verify_ticket("CB-1", "bug", ["f.py"], rework_count=0)
        assert r["decision"] == "REWORK"

    def test_max_rework_yields_human(self, tmp_path):
        ws = tmp_path / "ws"; ws.mkdir()
        gk = Gatekeeper(tmp_path / "state", policy_path=_policy(tmp_path, "false"), workspace=ws)
        r = gk.verify_ticket("CB-1", "bug", ["f.py"], rework_count=MAX_REWORK_ATTEMPTS)
        assert r["decision"] == "HUMAN_REQUIRED"

    def test_decision_logged(self, tmp_path):
        ws = tmp_path / "ws"; ws.mkdir()
        gk = Gatekeeper(tmp_path / "state", policy_path=_policy(tmp_path, "echo ok"), workspace=ws)
        gk.verify_ticket("CB-1", "bug", ["f.py"])
        log = tmp_path / "state" / "gatekeeper_log.jsonl"
        assert json.loads(log.read_text().strip())["ticket_id"] == "CB-1"

    def test_history(self, tmp_path):
        ws = tmp_path / "ws"; ws.mkdir()
        gk = Gatekeeper(tmp_path / "state", policy_path=_policy(tmp_path, "echo ok"), workspace=ws)
        gk.verify_ticket("CB-1", "bug", ["f.py"])
        gk.verify_ticket("CB-2", "bug", ["f.py"])
        assert len(gk.get_history("CB-1")) == 1

    def test_max_rework_is_3(self):
        assert MAX_REWORK_ATTEMPTS == 3
