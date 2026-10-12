"""The agent loop: register, heartbeat, claim, execute allowlisted workloads, report results."""
from __future__ import annotations

import logging
import platform
import threading
import time
from dataclasses import dataclass, field

from . import __version__, gpu, workloads
from .client import ApiError, MarketplaceClient, TransportError

log = logging.getLogger("sharedcompute.agent")
REHELLO_SECONDS = 600


@dataclass
class RunningJob:
    id: int
    workload: str
    params: dict
    gpu_slot: int | None
    time_limit: float
    started: float = field(default_factory=time.monotonic)
    cancel: threading.Event = field(default_factory=threading.Event)
    output: dict | None = None
    error: str | None = None
    cancelled: bool = False
    done: threading.Event = field(default_factory=threading.Event)

    def should_stop(self) -> bool:
        return self.cancel.is_set() or time.monotonic() - self.started > self.time_limit

    def run(self) -> None:
        try:
            runner = workloads.RUNNERS.get(self.workload)
            if runner is None:
                raise RuntimeError(f"Workload {self.workload} is not supported by this agent")
            self.output = runner(self.params, self.should_stop, self.gpu_slot)
        except workloads.Cancelled:
            self.cancelled = True
        except Exception as exc:  # reported to the marketplace as a job failure
            self.error = f"{type(exc).__name__}: {exc}"
        finally:
            self.done.set()


class Agent:
    def __init__(self, client: MarketplaceClient, *, poll_seconds: float = 2.0, max_jobs: int | None = None,
                 detector=gpu.detect, metrics=gpu.metrics, capabilities=workloads.capabilities):
        self.client = client
        self.poll_seconds = poll_seconds
        self.max_jobs = max_jobs
        self.detector, self.metrics, self.capabilities = detector, metrics, capabilities
        self.heartbeat_seconds = 10.0
        self.job: RunningJob | None = None
        self.jobs_done = 0
        self.stop = threading.Event()

    def hello_body(self) -> dict:
        return {"agent_version": __version__,
                "platform": f"{platform.system()} {platform.release()} {platform.machine()}",
                "capabilities": self.capabilities(), "max_concurrency": 1,
                "gpus": [g.to_dict() for g in self.detector()]}

    def hello(self) -> dict:
        reply = self.client.hello(self.hello_body())
        self.heartbeat_seconds = float(reply.get("heartbeat_interval_seconds", 10))
        gpus = ", ".join(f"{g['model']} ({'enabled' if g['enabled'] else 'paused'})" for g in reply["gpus"]) or "none"
        log.info("Registered as node %s (%s). GPUs: %s. Workloads: %s", reply["node_id"], reply["node_name"],
                 gpus, ", ".join(reply["capabilities"]) or "none")
        return reply

    def heartbeat(self) -> None:
        running = [self.job.id] if self.job and not self.job.done.is_set() else []
        reply = self.client.heartbeat({"gpu_metrics": self.metrics(), "running_job_ids": running})
        self.heartbeat_seconds = float(reply.get("heartbeat_interval_seconds", self.heartbeat_seconds))
        if self.job and self.job.id in reply.get("cancel_job_ids", []):
            log.info("Marketplace cancelled job %s; stopping it", self.job.id)
            self.job.cancel.set()

    def _start(self, spec: dict) -> None:
        self.job = RunningJob(id=spec["id"], workload=spec["workload"], params=spec["params"],
                              gpu_slot=spec.get("gpu_slot"), time_limit=float(spec["time_limit_seconds"]))
        log.info("Claimed job %s (%s)", self.job.id, self.job.workload)
        threading.Thread(target=self.job.run, name=f"job-{self.job.id}", daemon=True).start()

    def _report(self) -> None:
        job = self.job
        assert job is not None and job.done.is_set()
        runtime = time.monotonic() - job.started
        try:
            if job.output is not None:
                reply = self.client.complete(job.id, job.output, runtime)
                log.info("Job %s %s (%s)", job.id, reply["status"], reply.get("detail"))
            elif job.cancelled and job.cancel.is_set():
                log.info("Job %s stopped at the marketplace's request", job.id)
            elif job.cancelled:
                self.client.fail(job.id, "Exceeded the time limit on the provider", retryable=False)
            else:
                self.client.fail(job.id, job.error or "Unknown error", retryable=False)
                log.warning("Job %s failed: %s", job.id, job.error)
        except ApiError as exc:
            # 404/409: the marketplace already moved the job on. Anything else is logged and
            # dropped; the server's lease and liveness checks decide the job's fate. Network
            # errors (TransportError) propagate so the report is retried.
            log.warning("Report for job %s not accepted: %s", job.id, exc)
        self.job = None
        self.jobs_done += 1

    def run(self) -> None:
        backoff = 1.0
        last_hello = last_beat = 0.0
        while not self.stop.is_set():
            try:
                now = time.monotonic()
                if now - last_hello >= REHELLO_SECONDS or last_hello == 0.0:
                    self.hello()
                    last_hello = last_beat = time.monotonic()
                if time.monotonic() - last_beat >= self.heartbeat_seconds:
                    self.heartbeat()
                    last_beat = time.monotonic()
                if self.job and self.job.done.is_set():
                    self._report()
                if self.max_jobs is not None and self.jobs_done >= self.max_jobs:
                    break
                if self.job is None:
                    spec = self.client.claim()
                    if spec:
                        self._start(spec)
                        continue
                backoff = 1.0
            except TransportError as exc:
                log.warning("Marketplace unreachable (%s); retrying in %.0fs", exc, backoff)
                self.stop.wait(backoff)
                backoff = min(backoff * 2, 60.0)
                continue
            except ApiError as exc:
                if exc.status == 401:
                    log.error("Node token rejected (%s). Create or rotate a token in the provider dashboard.",
                              exc.message)
                    raise
                log.warning("Marketplace error: %s", exc)
            self.stop.wait(0.2 if self.job else self.poll_seconds)
        self._shutdown()

    def _shutdown(self) -> None:
        if self.job and not self.job.done.is_set():
            self.job.cancel.set()
            self.job.done.wait(10)
            try:
                self.client.fail(self.job.id, "Provider agent shut down", retryable=True)
            except (ApiError, TransportError):
                pass  # the server requeues the job once heartbeats stop
