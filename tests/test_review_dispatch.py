from pathlib import Path
from types import SimpleNamespace

from codebot.review_metrics import get_review_speed_quality_summary, record_reviewer_metric
from codebot.ticket_dispatcher import (
    rework_target_state,
    reviewer_roles_for_ticket,
    write_implementation_packet,
    write_review_packet,
)
from codebot.ticket_engine import TicketState


def _ticket(risk: str = "medium", ticket_class: str = "feature", modules: list[str] | None = None):
    return SimpleNamespace(
        id="CB-review",
        ticket_class=SimpleNamespace(value=ticket_class),
        risk=SimpleNamespace(value=risk),
        acceptance_criteria=["works"],
        desired_state="working",
        affected_modules=modules or ["codebot/module.py"],
        required_tests=["tests/test_module.py"],
        security_impact="none",
        attempts=1,
        repo_revision="",
    )


def test_low_risk_feature_uses_one_targeted_reviewer():
    assert reviewer_roles_for_ticket(_ticket("low")) == ("reviewer",)


def test_level_one_review_uses_only_the_reviewer():
    assert reviewer_roles_for_ticket(_ticket("critical", "security")) == ("reviewer",)


def test_review_packet_contains_only_ticket_scoped_context(tmp_path: Path):
    path = write_review_packet(_ticket(), tmp_path)
    content = path.read_text(encoding="utf-8")
    assert '"ticket_id": "CB-review"' in content
    assert '"required_tests"' in content


def test_implementation_packet_includes_targeted_test_context(tmp_path: Path):
    path = write_implementation_packet(_ticket(), tmp_path)
    content = path.read_text(encoding="utf-8")
    assert '"required_tests"' in content
    assert '"handoff"' in content


def test_rework_with_a_plan_returns_directly_to_implementation(tmp_path: Path):
    plans = tmp_path / "plans"
    plans.mkdir()
    (plans / "CB-review.plan.json").write_text("{}", encoding="utf-8")
    assert rework_target_state(_ticket(), plans) == TicketState.IMPLEMENT


def test_review_speed_quality_summary(tmp_path: Path, monkeypatch):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setenv("CODEBOT_STATE_DIR", str(state_dir))
    record_reviewer_metric("reviewer", "CB-1", "APPROVE", duration_s=2.0)
    record_reviewer_metric("reviewer", "CB-2", "REWORK", duration_s=6.0)
    assert get_review_speed_quality_summary(state_dir) == {
        "reviews": 2,
        "average_duration_s": 4.0,
        "p95_duration_s": 6.0,
        "rework_rate": 0.5,
    }
