"""Flat scheduler configuration for authoritative dispatch (ADR-007 §15).

No BacklogConfig / DiscoveryConfig / WorkforceConfig / Hysteresis hierarchy.
All values are module-level constants derived from environment variables.
"""

import os

MAX_CONCURRENT_AGENTS: int = int(os.getenv("CODEBOT_MAX_CONCURRENT", "90"))
SPAWN_STAGGER_SECONDS: float = 5.0
DISCOVERY_MAX: int = 3
SCHEDULER_INTERVAL_SECONDS: int = 30
WORKER_HEARTBEAT_TIMEOUT: int = 600
WORKER_LEASE_DURATION: int = 1800
WORKER_MAX_RETRIES: int = 3
HOURLY_LIMIT_USD: float = 10.0
DAILY_LIMIT_USD: float = 100.0
INTEGRATION_QUEUE_MAX: int = 20

BUCKET_WEIGHTS: dict[str, int] = {
    "USER": 1,
    "GOAL": 1,
    "DECOMP": 1,
    "PLANNING": 1,
    "REWORK": 2,
    "IMPLEMENT": 3,
    "REVIEW": 2,
    "VERIFY": 1,
}

ROLE_CAPS: dict[str, int] = {
    "triage": 10,
    "goal": 10,
    "verify": 5,
    "discovery": DISCOVERY_MAX,
}
