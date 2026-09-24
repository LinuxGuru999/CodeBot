"""User request envelope for the authoritative ingress pipeline.

Defines the durable request object that flows from external input (API/CLI)
through request_ingestion into a REQUESTED ticket, then through user_agent
evaluation to terminal state.

Context ownership: User Agent stage produces DESIRED-STATE context.
See docs/CODING_STANDARDS.md §22 and ADR-008.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Any


class UserRequestStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    AWAITING_INPUT = "awaiting_input"
    REJECTED = "rejected"
    COMPLETE = "complete"


_TRANSITIONS: dict[UserRequestStatus, frozenset[UserRequestStatus]] = {
    UserRequestStatus.PENDING: frozenset({UserRequestStatus.RUNNING}),
    UserRequestStatus.RUNNING: frozenset({
        UserRequestStatus.AWAITING_INPUT,
        UserRequestStatus.REJECTED,
        UserRequestStatus.COMPLETE,
    }),
    UserRequestStatus.AWAITING_INPUT: frozenset({UserRequestStatus.PENDING}),
    UserRequestStatus.REJECTED: frozenset(),
    UserRequestStatus.COMPLETE: frozenset(),
}


@dataclass(frozen=True)
class DedupCandidate:
    ticket_id: str
    title: str
    state: str
    similarity: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DedupCandidate:
        return cls(
            ticket_id=str(data.get("ticket_id", "")),
            title=str(data.get("title", "")),
            state=str(data.get("state", "")),
            similarity=float(data.get("similarity", 0.0)),
        )


@dataclass(frozen=True)
class UserRequest:
    request_id: str
    source: str
    message: str
    context: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    status: UserRequestStatus = UserRequestStatus.PENDING
    conversation_id: str = ""
    clarification_questions: tuple[str, ...] = ()
    response: str = ""
    rejection_reason: str = ""
    findings_emitted: tuple[str, ...] = ()
    dedup_candidates: tuple[DedupCandidate, ...] = ()

    def __post_init__(self) -> None:
        if not self.conversation_id:
            object.__setattr__(self, "conversation_id", self.request_id)
        if self.created_at == 0.0:
            now = time.time()
            object.__setattr__(self, "created_at", now)
            object.__setattr__(self, "updated_at", now)

    def _validate_transition(self, new_status: UserRequestStatus) -> None:
        allowed = _TRANSITIONS.get(self.status, frozenset())
        if new_status not in allowed:
            raise ValueError(
                f"invalid transition {self.status.value} -> {new_status.value} "
                f"(allowed: {sorted(s.value for s in allowed)})"
            )

    def update_status(self, new_status: UserRequestStatus) -> UserRequest:
        self._validate_transition(new_status)
        return UserRequest(
            request_id=self.request_id,
            source=self.source,
            message=self.message,
            context=self.context,
            created_at=self.created_at,
            updated_at=time.time(),
            status=new_status,
            conversation_id=self.conversation_id,
            clarification_questions=self.clarification_questions,
            response=self.response,
            rejection_reason=self.rejection_reason,
            findings_emitted=self.findings_emitted,
            dedup_candidates=self.dedup_candidates,
        )

    def with_response(self, response: str) -> UserRequest:
        return UserRequest(
            request_id=self.request_id,
            source=self.source,
            message=self.message,
            context=self.context,
            created_at=self.created_at,
            updated_at=time.time(),
            status=self.status,
            conversation_id=self.conversation_id,
            clarification_questions=(),
            response=response,
            rejection_reason=self.rejection_reason,
            findings_emitted=self.findings_emitted,
            dedup_candidates=self.dedup_candidates,
        )

    def with_clarification_questions(self, questions: tuple[str, ...]) -> UserRequest:
        return UserRequest(
            request_id=self.request_id,
            source=self.source,
            message=self.message,
            context=self.context,
            created_at=self.created_at,
            updated_at=time.time(),
            status=self.status,
            conversation_id=self.conversation_id,
            clarification_questions=questions,
            response=self.response,
            rejection_reason=self.rejection_reason,
            findings_emitted=self.findings_emitted,
            dedup_candidates=self.dedup_candidates,
        )

    def reject(self, reason: str) -> UserRequest:
        self._validate_transition(UserRequestStatus.REJECTED)
        return UserRequest(
            request_id=self.request_id,
            source=self.source,
            message=self.message,
            context=self.context,
            created_at=self.created_at,
            updated_at=time.time(),
            status=UserRequestStatus.REJECTED,
            conversation_id=self.conversation_id,
            clarification_questions=self.clarification_questions,
            response=self.response,
            rejection_reason=reason,
            findings_emitted=self.findings_emitted,
            dedup_candidates=self.dedup_candidates,
        )

    def complete(self, findings: tuple[str, ...]) -> UserRequest:
        self._validate_transition(UserRequestStatus.COMPLETE)
        return UserRequest(
            request_id=self.request_id,
            source=self.source,
            message=self.message,
            context=self.context,
            created_at=self.created_at,
            updated_at=time.time(),
            status=UserRequestStatus.COMPLETE,
            conversation_id=self.conversation_id,
            clarification_questions=self.clarification_questions,
            response=self.response,
            rejection_reason=self.rejection_reason,
            findings_emitted=findings,
            dedup_candidates=self.dedup_candidates,
        )

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        d["dedup_candidates"] = [c.to_dict() for c in self.dedup_candidates]
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> UserRequest:
        status_raw = data.get("status", "pending")
        try:
            status = UserRequestStatus(status_raw)
        except ValueError:
            status = UserRequestStatus.PENDING

        raw_candidates = data.get("dedup_candidates", ())
        candidates: tuple[DedupCandidate, ...] = ()
        if raw_candidates:
            candidates = tuple(
                DedupCandidate.from_dict(c) if isinstance(c, dict) else c
                for c in raw_candidates
            )

        questions = data.get("clarification_questions", ())
        if isinstance(questions, list):
            questions = tuple(questions)

        findings = data.get("findings_emitted", ())
        if isinstance(findings, list):
            findings = tuple(findings)

        return cls(
            request_id=str(data.get("request_id", "")),
            source=str(data.get("source", "user")),
            message=str(data.get("message", "")),
            context=str(data.get("context", "")),
            created_at=float(data.get("created_at", 0.0)),
            updated_at=float(data.get("updated_at", 0.0)),
            status=status,
            conversation_id=str(data.get("conversation_id", "")),
            clarification_questions=questions,
            response=str(data.get("response", "")),
            rejection_reason=str(data.get("rejection_reason", "")),
            findings_emitted=findings,
            dedup_candidates=candidates,
        )

    def to_json(self, pretty: bool = False) -> str:
        indent = 2 if pretty else None
        return json.dumps(self.to_dict(), indent=indent)

    @staticmethod
    def ensure_directories(state_dir: Path) -> None:
        (state_dir / "requests").mkdir(parents=True, exist_ok=True)
        (state_dir / "requests" / "rejected").mkdir(parents=True, exist_ok=True)
        (state_dir / "requests" / "processed").mkdir(parents=True, exist_ok=True)

    @staticmethod
    def generate_id() -> str:
        return uuid.uuid4().hex[:16].upper()
