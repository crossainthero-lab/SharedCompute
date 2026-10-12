"""Runtime settings, read from SHAREDCOMPUTE_* environment variables."""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return default if raw is None else int(raw)


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return default if raw is None else float(raw)


@dataclass
class Settings:
    db_path: Path = ROOT / "data" / "sharedcompute.sqlite3"

    # Economics. Fee is in basis points: 1000 = 10% marketplace, 90% provider.
    platform_fee_bps: int = 1000

    # Scheduling and liveness.
    max_attempts: int = 3
    node_ttl_seconds: float = 30.0          # agent heartbeat freshness window
    claim_timeout_seconds: float = 90.0     # agent must claim an assigned job within this
    time_limit_grace_seconds: float = 60.0  # slack on top of the job's estimated minutes
    allow_self_match: bool = True           # let a user's job run on their own GPU

    # Simulation (sample network + self-reported listings).
    simulation_seconds: float = 8.0
    seed_sample_network: bool = True
    demo_credits_enabled: bool = True

    # Real agent workloads.
    hashchain_iterations: int = 250_000

    # Exposure. `serve` tightens these automatically when bound to anything but loopback.
    registration_open: bool = True     # anyone may create an account
    public_marketplace: bool = True    # listings and quotes readable without signing in
    expose_api_docs: bool = True       # /api/docs and /api/openapi.json

    # Auth.
    password_iterations: int = 600_000
    session_ttl_seconds: int = 14 * 24 * 3600
    cookie_secure: bool = False
    login_attempts_per_minute: int = 10

    # Background loop.
    run_background_loop: bool = True
    tick_interval_seconds: float = 1.0

    clock: Callable[[], float] = field(default=time.time, repr=False)

    @classmethod
    def from_env(cls) -> "Settings":
        s = cls()
        if os.environ.get("SHAREDCOMPUTE_DB"):
            s.db_path = Path(os.environ["SHAREDCOMPUTE_DB"]).expanduser()
        s.platform_fee_bps = _int("SHAREDCOMPUTE_PLATFORM_FEE_BPS", s.platform_fee_bps)
        s.max_attempts = _int("SHAREDCOMPUTE_MAX_ATTEMPTS", s.max_attempts)
        s.node_ttl_seconds = _float("SHAREDCOMPUTE_NODE_TTL_SECONDS", s.node_ttl_seconds)
        s.claim_timeout_seconds = _float("SHAREDCOMPUTE_CLAIM_TIMEOUT_SECONDS", s.claim_timeout_seconds)
        s.allow_self_match = _bool("SHAREDCOMPUTE_ALLOW_SELF_MATCH", s.allow_self_match)
        s.simulation_seconds = _float("SHAREDCOMPUTE_SIMULATION_SECONDS", s.simulation_seconds)
        s.seed_sample_network = _bool("SHAREDCOMPUTE_SEED_SAMPLES", s.seed_sample_network)
        s.demo_credits_enabled = _bool("SHAREDCOMPUTE_DEMO_CREDITS", s.demo_credits_enabled)
        s.hashchain_iterations = _int("SHAREDCOMPUTE_HASHCHAIN_ITERATIONS", s.hashchain_iterations)
        s.password_iterations = _int("SHAREDCOMPUTE_PASSWORD_ITERATIONS", s.password_iterations)
        s.cookie_secure = _bool("SHAREDCOMPUTE_COOKIE_SECURE", s.cookie_secure)
        if os.environ.get("SHAREDCOMPUTE_REGISTRATION"):
            mode = os.environ["SHAREDCOMPUTE_REGISTRATION"].strip().lower()
            if mode not in ("open", "closed"):
                raise ValueError("SHAREDCOMPUTE_REGISTRATION must be 'open' or 'closed'")
            s.registration_open = mode == "open"
        s.public_marketplace = _bool("SHAREDCOMPUTE_PUBLIC_MARKETPLACE", s.public_marketplace)
        s.expose_api_docs = _bool("SHAREDCOMPUTE_API_DOCS", s.expose_api_docs)
        s.validate()
        return s

    def validate(self) -> None:
        if not 0 <= self.platform_fee_bps <= 10_000:
            raise ValueError("platform_fee_bps must be between 0 and 10000")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")

    def now(self) -> float:
        return self.clock()
