"""Scheduler V2 package."""

from .bucket_dispatcher import (
    BUCKET_ORDER,
    BUCKET_TO_ROLE_SETS,
    BucketDispatcher,
    BucketSnapshot,
    NoModelsAvailableError,
    TICKET_CLASS_TO_ROLES,
)
from .dispatcher import InvariantViolation, ModelSelector, ReasonCode, Scheduler
from .dispatch_gate import (
    ClaimOutcome,
    ClaimRecord,
    ConcurrencyController,
    DispatchGate,
    DispatchResult,
    ScheduledSpawn,
    SlotToken,
    SPAWN_STAGGER_SECONDS,
    SpawnQueue,
    SpawnRequest,
    claim_ticket,
    release_claim,
)
from .lifecycle import (
    AgentRecord,
    AgentState,
    InvalidTransitionError,
    get_stale_timeout,
    is_agent_stale,
)

__all__ = [
    "AgentRecord",
    "AgentState",
    "BUCKET_ORDER",
    "BUCKET_TO_ROLE_SETS",
    "BucketDispatcher",
    "BucketSnapshot",
    "InvalidTransitionError",
    "InvariantViolation",
    "get_stale_timeout",
    "is_agent_stale",
    "ClaimOutcome",
    "ClaimRecord",
    "ConcurrencyController",
    "DispatchGate",
    "DispatchResult",
    "ModelSelector",
    "NoModelsAvailableError",
    "ReasonCode",
    "ScheduledSpawn",
    "Scheduler",
    "SlotToken",
    "SPAWN_STAGGER_SECONDS",
    "SpawnQueue",
    "SpawnRequest",
    "TICKET_CLASS_TO_ROLES",
    "claim_ticket",
    "release_claim",
]
