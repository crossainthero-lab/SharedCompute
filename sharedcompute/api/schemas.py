"""Request bodies. Pydantic handles shape and types; services enforce ranges and business rules."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict

Usd = str | int | float


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RegisterIn(Strict):
    email: str
    password: str
    display_name: str


class LoginIn(Strict):
    email: str
    password: str


class JobIn(Strict):
    name: str
    workload: str
    min_vram_gb: int
    est_minutes: int
    max_budget_usd: Usd
    region: str = "any"


class QuoteIn(Strict):
    workload: str
    min_vram_gb: int
    est_minutes: int
    max_budget_usd: Usd
    region: str = "any"


class TopupIn(Strict):
    amount_usd: Usd


class NodeIn(Strict):
    kind: str
    name: str
    region: str
    hourly_usd: Usd
    gpu_model: str | None = None   # simulated listings only
    vram_gb: int | None = None     # simulated listings only


class NodePatch(Strict):
    name: str | None = None
    region: str | None = None
    enabled: bool | None = None


class GpuPatch(Strict):
    hourly_usd: Usd | None = None
    enabled: bool | None = None


class GpuReport(BaseModel):
    slot: int
    vendor: str
    model: str
    vram_mb: int
    detection: str
    fingerprint: str | None = None
    cuda: bool = False
    compute_capability: str | None = None
    driver_version: str | None = None
    integrated: bool = False


class HelloIn(BaseModel):
    agent_version: str
    platform: str | None = None
    capabilities: list[str] = []
    max_concurrency: int = 1
    gpus: list[GpuReport] = []


class GpuMetric(BaseModel):
    slot: int
    util_percent: float | None = None
    mem_used_mb: int | None = None
    temperature_c: float | None = None


class HeartbeatIn(BaseModel):
    gpu_metrics: list[GpuMetric] = []
    running_job_ids: list[int] = []


class CompleteIn(BaseModel):
    output: dict
    runtime_seconds: float | None = None


class FailIn(BaseModel):
    reason: str = "Agent reported a failure"
    retryable: bool = False
