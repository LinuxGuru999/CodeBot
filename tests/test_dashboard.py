from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import MagicMock, patch


def test_live_snapshot_collects_the_botop_live_feed_data(tmp_path: Path) -> None:
    from codebot.dashboard import live_snapshot

    agents = [{"name": "planner", "bucket": "RUNNING", "hb_age": 2.0}]
    tickets = [{"id": "CB-1", "state": "READY", "severity": "high", "title": "Ship dashboard"}]

    with (
        patch("codebot.dashboard.botop._find_project_name", return_value="CodeBot"),
        patch("codebot.dashboard.botop._find_state_dir", return_value=tmp_path / ".codebot"),
        patch("codebot.dashboard.botop._find_logs_dir", return_value=tmp_path / "logs"),
        patch("codebot.dashboard.botop._collect_orchestrator_info", return_value={"alive": True}),
        patch("codebot.dashboard.botop._collect_agents", return_value=agents),
        patch("codebot.dashboard.botop._collect_tickets", return_value=(None, {"READY": 1}, tickets)),
        patch("codebot.dashboard.botop._collect_claims", return_value=[]),
        patch("codebot.dashboard.botop._implementation_claims", return_value=[]),
        patch("codebot.dashboard.botop._collect_ticket_throughput", return_value={"total": 1}),
        patch("codebot.dashboard.botop._collect_token_ledger", return_value={"total_actual": 12}),
        patch("codebot.dashboard.botop._collect_budget_state", return_value=("ok", 12)),
    ):
        snapshot = live_snapshot(tmp_path)

    assert snapshot["project"] == "CodeBot"
    assert snapshot["state_dir"] == str(tmp_path / ".codebot")
    assert snapshot["logs_dir"] == "logs"
    assert snapshot["agents"]["total"] == 1
    assert snapshot["agents"]["items"] == agents
    assert snapshot["tickets"]["summary"] == {"READY": 1}
    assert snapshot["tickets"]["groups"]["READY"]["count"] == 1
    assert snapshot["tickets"]["groups"]["READY"]["items"][0]["id"] == "CB-1"
    assert snapshot["tickets"]["groups"]["DISCOVERED"] == {"count": 0, "items": []}
    assert snapshot["budget"] == {"state": "ok", "total": 12}


def test_live_snapshot_keeps_summary_counts_without_ticket_details(tmp_path: Path) -> None:
    from codebot.dashboard import live_snapshot

    with (
        patch("codebot.dashboard.botop._find_project_name", return_value="CodeBot"),
        patch("codebot.dashboard.botop._collect_orchestrator_info", return_value={}),
        patch("codebot.dashboard.botop._collect_agents", return_value=[]),
        patch("codebot.dashboard.botop._collect_tickets", return_value=(None, {"TRIAGED": 2, "REJECTED": 1}, [])),
        patch("codebot.dashboard.botop._collect_claims", return_value=[]),
        patch("codebot.dashboard.botop._implementation_claims", return_value=[]),
        patch("codebot.dashboard.botop._collect_ticket_throughput", return_value={"total": 3}),
        patch("codebot.dashboard.botop._collect_token_ledger", return_value=None),
    ):
        snapshot = live_snapshot(tmp_path)

    groups = snapshot["tickets"]["groups"]
    assert groups["TRIAGED"] == {"count": 2, "items": []}
    assert groups["REJECTED"] == {"count": 1, "items": []}


def test_ticket_explorer_pages_a_stage_and_returns_recorded_work(tmp_path: Path) -> None:
    from codebot.dashboard import ticket_explorer_detail, ticket_explorer_snapshot

    tickets = [
        {
            "id": "CB-2",
            "state": "TRIAGED",
            "severity": "high",
            "title": "Second ticket",
            "updated_at": 2,
            "gate_history": [{"gate": "tests", "passed": True}],
        },
        {
            "id": "CB-1",
            "state": "TRIAGED",
            "severity": "low",
            "title": "First ticket",
            "updated_at": 1,
            "reviewer_feedback": [{"summary": "Ready"}],
        },
        {"id": "CB-3", "state": "COMPLETE", "severity": "medium", "title": "Closed ticket"},
    ]
    (tmp_path / "lifecycle_events.jsonl").write_text(
        '{"ticket_id":"CB-2","from_state":"READY","to_state":"TRIAGED","timestamp":4}\n',
        encoding="utf-8",
    )

    with (
        patch("codebot.dashboard.botop._collect_tickets", return_value=(None, {"TRIAGED": 2, "COMPLETE": 1}, tickets)),
        patch("codebot.dashboard.botop._find_state_dir", return_value=tmp_path),
    ):
        snapshot = ticket_explorer_snapshot(tmp_path, state="TRIAGED", limit=1)
        detail = ticket_explorer_detail(tmp_path, "CB-2")

    assert snapshot["matching"] == 2
    items = snapshot["items"]
    assert isinstance(items, list)
    assert isinstance(items[0], dict)
    assert items[0]["id"] == "CB-2"
    assert snapshot["states"] == [{"name": "TRIAGED", "count": 2}, {"name": "COMPLETE", "count": 1}]
    assert detail is not None
    work = detail["work"]
    assert isinstance(work, dict)
    lifecycle = work["lifecycle"]
    assert isinstance(lifecycle, list)
    assert isinstance(lifecycle[0], dict)
    assert lifecycle[0]["from_state"] == "READY"
    assert work["gate_history"] == [{"gate": "tests", "passed": True}]


def test_ticket_explorer_collapses_legacy_lifecycle_aliases(tmp_path: Path) -> None:
    from codebot.dashboard import ticket_explorer_snapshot

    tickets = [{"id": "CB-1", "state": "IMPLEMENTING", "severity": "high", "title": "Legacy state"}]
    with patch("codebot.dashboard.botop._collect_tickets", return_value=(None, {"IMPLEMENTING": 1}, tickets)):
        snapshot = ticket_explorer_snapshot(tmp_path, state="IMPLEMENT")

    assert snapshot["matching"] == 1
    assert snapshot["states"] == [{"name": "IMPLEMENT", "count": 1}]


def test_ticket_explorer_page_has_lifecycle_navigation() -> None:
    from codebot.dashboard import ticket_explorer_page

    page = ticket_explorer_page().decode()

    assert 'id="stages"' in page
    assert 'id="ticket-list"' in page
    assert 'id="detail"' in page


def test_dashboard_snapshot_is_public_for_loopback_clients() -> None:
    from codebot.control_server import ControlHandler

    shell = MagicMock(spec=ControlHandler)
    shell.path = "/dashboard"
    shell.client_address = ("127.0.0.1", 12345)
    shell.headers = {}
    shell._html = MagicMock()
    shell._json = MagicMock()
    shell.rfile = io.BytesIO(b"")
    shell.wfile = io.BytesIO()
    shell.requestline = "GET /dashboard HTTP/1.1"
    shell.request_version = "HTTP/1.1"
    shell.command = "GET"

    ControlHandler.do_GET(shell)

    shell._html.assert_called_once()
    shell._json.assert_not_called()

    snapshot = MagicMock(spec=ControlHandler)
    snapshot.path = "/dashboard/snapshot"
    snapshot.client_address = ("127.0.0.1", 12345)
    snapshot.headers = {}
    snapshot._json = MagicMock()
    snapshot._is_loopback_client = MagicMock(return_value=True)
    snapshot.rfile = io.BytesIO(b"")
    snapshot.wfile = io.BytesIO()
    snapshot.requestline = "GET /dashboard/snapshot HTTP/1.1"
    snapshot.request_version = "HTTP/1.1"
    snapshot.command = "GET"

    ControlHandler.do_GET(snapshot)

    snapshot._is_loopback_client.assert_called_once()
    snapshot._json.assert_called_once()
