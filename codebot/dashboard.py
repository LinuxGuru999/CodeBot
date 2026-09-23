from __future__ import annotations

import json
import time
from collections import deque
from collections.abc import Mapping
from pathlib import Path
from typing import TypeAlias, TypedDict

from codebot import botop


JsonValue: TypeAlias = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
JsonMapping: TypeAlias = Mapping[str, JsonValue]
JsonPayload: TypeAlias = dict[str, JsonValue]


class TicketItem(TypedDict):
    id: str
    severity: str
    title: str


class TicketGroup(TypedDict):
    count: int
    items: list[TicketItem]


class AgentsSnapshot(TypedDict):
    total: int
    items: list[JsonPayload]
    implementation: list[JsonPayload]


class TicketsSnapshot(TypedDict):
    summary: dict[str, int]
    throughput: JsonPayload
    groups: dict[str, TicketGroup]


class BudgetSnapshot(TypedDict):
    state: str
    total: int | None


class LiveSnapshot(TypedDict):
    version: int
    generated_at: float
    project: str
    state_dir: str
    logs_dir: str
    orchestrator: JsonPayload
    agents: AgentsSnapshot
    tickets: TicketsSnapshot
    budget: BudgetSnapshot


_TICKET_STATES = ("TRIAGED", "GOAL", "DECOMP", "PLANNING", "IMPLEMENT", "REVIEW", "COMPLETE")
_EXPLORER_STATES = (
    "DISCOVERED", "TRIAGED", "GOAL", "DECOMP", "PLANNING",
    "IMPLEMENT", "REVIEW", "REWORK", "BLOCKED", "DEFERRED",
    "COMPLETE", "REJECTED", "DUPLICATE", "NOT_ACTIONABLE",
    "RESOLVED", "SUPERSEDED", "CANCELLED", "LATER", "NEVER",
)
_LEGACY_STATE_ALIASES = {"DECOMPOSE": "DECOMP", "IMPLEMENTING": "IMPLEMENT", "REVIEWING": "REVIEW"}


def _text(value: JsonValue) -> str:
    return value if isinstance(value, str) else str(value)


def _number(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _json_value(value: object) -> JsonValue:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return str(value)


def _mapping(value: Mapping[str, object]) -> JsonPayload:
    return {str(key): _json_value(item) for key, item in value.items()}


def _ticket_value(ticket: JsonMapping, field: str) -> str:
    return _text(ticket.get(field, ""))


def _canonical_state(value: str) -> str:
    normalized = value.upper()
    return _LEGACY_STATE_ALIASES.get(normalized, normalized)


def _ticket_mapping(ticket: object) -> JsonPayload:
    if isinstance(ticket, Mapping):
        return {str(key): _json_value(value) for key, value in ticket.items()}
    to_dict = getattr(ticket, "to_dict", None)
    if callable(to_dict):
        value = to_dict()
        if isinstance(value, Mapping):
            return _mapping(value)
    return {}


def _timestamp(value: JsonValue) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return 0.0
    return 0.0


def _all_tickets(project_root: Path) -> tuple[dict[str, int], list[JsonPayload]]:
    _, raw_summary, raw_tickets = botop._collect_tickets(project_root)
    summary: dict[str, int] = {}
    for key, value in (raw_summary or {}).items():
        state = _canonical_state(str(key))
        summary[state] = summary.get(state, 0) + _number(value)
    tickets = [ticket for raw in raw_tickets if (ticket := _ticket_mapping(raw))]
    for ticket in tickets:
        ticket["state"] = _canonical_state(_ticket_value(ticket, "state"))
    return summary, tickets


def _ticket_groups(tickets: list[JsonPayload], summary: dict[str, int]) -> dict[str, TicketGroup]:
    groups: dict[str, TicketGroup] = {}
    states = (*_EXPLORER_STATES, *(state for state in summary if state not in _EXPLORER_STATES))
    for state in states:
        items = [ticket for ticket in tickets if _ticket_value(ticket, "state").upper() == state]
        groups[state] = {
            "count": summary.get(state, len(items)),
            "items": [
                {
                    "id": _ticket_value(ticket, "id")[:64],
                    "severity": _ticket_value(ticket, "severity")[:32],
                    "title": _ticket_value(ticket, "title")[:160],
                }
                for ticket in items[:3]
            ],
        }
    return groups


def live_snapshot(project_root: Path) -> LiveSnapshot:
    raw_agents = botop._collect_agents(project_root)
    raw_summary, tickets = _all_tickets(project_root)
    raw_claims = botop._collect_claims(project_root)
    ledger = botop._collect_token_ledger(project_root)
    agents = _without_internal_fields(raw_agents)
    summary = raw_summary
    throughput = _mapping(botop._collect_ticket_throughput(tickets))
    orchestrator = _mapping(botop._collect_orchestrator_info(project_root))
    claims = [_mapping(claim) for claim in botop._implementation_claims(raw_agents, raw_claims)]
    budget_state, budget_total = botop._collect_budget_state(ledger) if ledger else ("unknown", None)
    return {
        "version": 1,
        "generated_at": time.time(),
        "project": botop._find_project_name(project_root),
        "state_dir": str(botop._find_state_dir(project_root)),
        "logs_dir": botop._find_logs_dir(project_root).name,
        "orchestrator": orchestrator,
        "agents": {"total": len(agents), "items": agents[:15], "implementation": claims[:6]},
        "tickets": {"summary": summary, "throughput": throughput, "groups": _ticket_groups(tickets, summary)},
        "budget": {"state": budget_state, "total": budget_total},
    }


def dashboard_page() -> bytes:
    return (Path(__file__).parent / "static" / "dashboard.html").read_bytes()


def ticket_explorer_page() -> bytes:
    return (Path(__file__).parent / "static" / "tickets.html").read_bytes()


def _bounded_int(value: object, *, default: int, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(int(str(value)), maximum))
    except (TypeError, ValueError):
        return default


def _ticket_summary(ticket: JsonMapping) -> JsonPayload:
    return {
        "id": _ticket_value(ticket, "id")[:64],
        "title": _ticket_value(ticket, "title")[:240],
        "state": _canonical_state(_ticket_value(ticket, "state")),
        "severity": _ticket_value(ticket, "severity")[:32],
        "ticket_class": _ticket_value(ticket, "ticket_class")[:64],
        "assigned_agent": _ticket_value(ticket, "assigned_agent")[:128],
        "updated_at": ticket.get("updated_at", 0),
        "attempts": ticket.get("attempts", 0),
        "rework_count": ticket.get("rework_count", 0),
    }


def ticket_explorer_snapshot(
    project_root: Path, *, state: str = "", query: str = "", offset: object = 0, limit: object = 100,
) -> JsonPayload:
    summary, tickets = _all_tickets(project_root)
    normalized_state = state.strip().upper()
    normalized_query = query.strip().lower()
    filtered = [
        ticket for ticket in tickets
        if (not normalized_state or _canonical_state(_ticket_value(ticket, "state")) == normalized_state)
        and (not normalized_query or normalized_query in _ticket_value(ticket, "id").lower() or normalized_query in _ticket_value(ticket, "title").lower())
    ]
    filtered.sort(key=lambda ticket: _timestamp(ticket.get("updated_at", 0)), reverse=True)
    page_offset = _bounded_int(offset, default=0, minimum=0, maximum=len(filtered))
    page_limit = _bounded_int(limit, default=100, minimum=1, maximum=200)
    states = (*_EXPLORER_STATES, *(key for key in summary if key not in _EXPLORER_STATES))
    counts = {name: summary.get(name, sum(_canonical_state(_ticket_value(ticket, "state")) == name for ticket in tickets)) for name in states}
    return {
        "generated_at": time.time(),
        "total": len(tickets),
        "states": [{"name": name, "count": count} for name, count in counts.items() if count],
        "selected_state": normalized_state,
        "query": query[:120],
        "offset": page_offset,
        "limit": page_limit,
        "matching": len(filtered),
        "items": [_ticket_summary(ticket) for ticket in filtered[page_offset:page_offset + page_limit]],
    }


def _ticket_events(state_dir: Path, ticket_id: str) -> list[JsonPayload]:
    events_path = state_dir / "lifecycle_events.jsonl"
    if not events_path.exists():
        return []
    events: deque[JsonPayload] = deque(maxlen=100)
    try:
        with events_path.open(encoding="utf-8", errors="ignore") as events_file:
            for line in events_file:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, Mapping) and event.get("ticket_id") == ticket_id:
                    events.append({str(key): _json_value(value) for key, value in event.items()})
    except OSError:
        return []
    return list(reversed(events))


def ticket_explorer_detail(project_root: Path, ticket_id: str) -> JsonPayload | None:
    _, tickets = _all_tickets(project_root)
    ticket = next((candidate for candidate in tickets if _ticket_value(candidate, "id") == ticket_id), None)
    if ticket is None:
        return None
    fields = (
        "evidence", "problem_statement", "desired_state", "acceptance_criteria", "affected_modules",
        "dependencies", "required_tests", "documentation_requirements", "outcome", "commit_sha", "pr_url",
        "risk", "assigned_agent", "assigned_model", "created_at", "updated_at", "attempts", "rework_count",
    )
    details: JsonPayload = {field: ticket.get(field, "") for field in fields}
    work: JsonPayload = {
        "lifecycle": [_json_value(event) for event in _ticket_events(botop._find_state_dir(project_root), ticket_id)],
        "gate_history": ticket.get("gate_history", []),
        "reviewer_feedback": ticket.get("reviewer_feedback", []),
    }
    return {
        "ticket": _ticket_summary(ticket),
        "details": details,
        "work": work,
    }


def _without_internal_fields(agents: list[JsonPayload]) -> list[JsonPayload]:
    return [{key: value for key, value in agent.items() if key not in {"ckpt", "state", "status", "scratch"}} for agent in agents]
