"""The agent loop: register, heartbeat, claim, execute allowlisted workloads, report results."""
from __future__ import annotations

import logging
import platform
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from . import __version__, gpu, workloads
from .client import ApiError, MarketplaceClient, TransportError
from .errors import Cancelled, Incompatible
from .policy import OfferPolicy

log = logging.getLogger("sharedcompute.agent")
REHELLO_SECONDS = 600


@dataclass
class RunningJob:
    id: int
    workload: str
    params: dict
    device: object
    options: workloads.RunOptions
    time_limit: float
    started: float = field(default_factory=time.monotonic)
    cancel: threading.Event = field(default_factory=threading.Event)
    output: Optional[dict] = None
    error: Optional[str] = None
    incompatible: Optional[str] = None
    cancelled: bool = False
    done: threading.Event = field(default_factory=threading.Event)

    def should_stop(self) -> bool:
        return self.cancel.is_set() or time.monotonic() - self.started > self.time_limit

    def run(self) -> None:
        try:
            runner = workloads.RUNNERS.get(self.workload)
            if runner is None:
                raise Incompatible(f"workload {self.workload} is not supported by this agent")
            self.output = runner(self.params, self.should_stop, self.device, self.options)
        except Cancelled:
            self.cancelled = True
        except Incompatible as exc:
            self.incompatible = str(exc)
        except Exception as exc:  # reported to the marketplace as a job failure
            self.error = f"{type(exc).__name__}: {exc}"
        finally:
            self.done.set()


class Agent:
    def __init__(self, client: MarketplaceClient, *, policy: Optional[OfferPolicy] = None,
                 options: Optional[workloads.RunOptions] = None, poll_seconds: float = 2.0,
                 max_jobs: Optional[int] = None, detector: Callable[[], list] = gpu.detect,
                 cpu_detector: Callable[[], object] = gpu.cpu_device, metrics: Callable[[], list] = gpu.metrics):
        self.client = client
        self.policy = policy or OfferPolicy()
        self.options = options or workloads.RunOptions()
        self.poll_seconds = poll_seconds
        self.max_jobs = max_jobs
        self.detector, self.cpu_detector, self.metrics = detector, cpu_detector, metrics
        self.heartbeat_seconds = 10.0
        self.devices: List = []
        self.job: Optional[RunningJob] = None
        self.jobs_done = 0
        self.stop = threading.Event()

    def inventory(self) -> List:
        devices = list(self.detector()) + [self.cpu_detector()]
        for warning in self.policy.apply(devices):
            log.warning(warning)
        self.devices = devices
        return devices

    def hello_body(self) -> dict:
        return {"agent_version": __version__,
                "platform": f"{platform.system()} {platform.release()} {platform.machine()}",
                "max_concurrency": 1, "gpus": [d.to_report() for d in self.inventory()]}

    def hello(self) -> dict:
        reply = self.client.hello(self.hello_body())
        self.heartbeat_seconds = float(reply.get("heartbeat_interval_seconds", 10))
        offered = [d for d in reply["gpus"] if d["offered"]]
        log.info("Registered as node %s (%s). Offered: %s", reply["node_id"], reply["node_name"],
                 ", ".join(f"{d['model']} [{', '.join(d['workloads']) or 'no workloads'}]" for d in offered) or "nothing")
        for d in reply["gpus"]:
            for workload, why in d.get("rejected_workloads", {}).items():
                log.warning("Marketplace will not send %s to %s: %s", workload, d["model"], "; ".join(why))
        if not offered:
            log.warning("No device is offered, so no jobs will arrive. Use --gpu INDEX and/or --offer-cpu.")
        return reply

    def heartbeat(self) -> None:
        running = [self.job.id] if self.job and not self.job.done.is_set() else []
        reply = self.client.heartbeat({"gpu_metrics": self.metrics(), "running_job_ids": running})
        self.heartbeat_seconds = float(reply.get("heartbeat_interval_seconds", self.heartbeat_seconds))
        if self.job and self.job.id in reply.get("cancel_job_ids", []):
            log.info("Marketplace cancelled job %s; stopping it", self.job.id)
            self.job.cancel.set()

    def _device_for(self, spec: dict):
        want = spec.get("device") or {"type": "gpu", "slot": spec.get("gpu_slot")}
        for d in self.devices:
            if d.device_type == want.get("type") and d.slot == want.get("slot"):
                return d
        return None

    def _start(self, spec: dict) -> None:
        device = self._device_for(spec)
        job = RunningJob(id=spec["id"], workload=spec["workload"], params=spec["params"], device=device,
                         options=self.options, time_limit=float(spec["time_limit_seconds"]))
        self.job = job
        # Re-check locally before doing any work: the marketplace's view of this machine may be stale.
        if device is None or not device.offered:
            job.incompatible = "the assigned device is not offered on this machine"
        elif spec["workload"] not in device.workloads:
            job.incompatible = f"{device.model} cannot run {spec['workload']}"
        if job.incompatible:
            job.done.set()
            return
        log.info("Claimed job %s (%s) on %s %s", job.id, job.workload, device.device_type.upper(), device.model)
        threading.Thread(target=job.run, name=f"job-{job.id}", daemon=True).start()

    def _report(self) -> None:
        job = self.job
        assert job is not None and job.done.is_set()
        runtime = time.monotonic() - job.started
        try:
            if job.incompatible:
                log.warning("Declining job %s: %s", job.id, job.incompatible)
                if job.device is not None and job.workload in job.device.workloads:
                    job.device.workloads.remove(job.workload)  # stop advertising it until the next hello (≤10 min)
                self.client.fail(job.id, job.incompatible, retryable=True, code="incompatible")
            elif job.output is not None:
                ran_on = (job.output.get("device") or {}).get("type", "?")
                reply = self.client.complete(job.id, job.output, runtime)
                log.info("Job %s ran on %s: %s (%s)", job.id, ran_on.upper(), reply["status"], reply.get("detail"))
            elif job.cancelled and job.cancel.is_set():
                log.info("Job %s stopped at the marketplace's request", job.id)
            elif job.cancelled:
                self.client.fail(job.id, "Exceeded the time limit on the provider", retryable=False)
            else:
                self.client.fail(job.id, job.error or "Unknown error", retryable=False)
                log.warning("Job %s failed: %s", job.id, job.error)
        except ApiError as exc:
            # 404/409: the marketplace already moved the job on. Anything else is logged and dropped;
            # the server's lease and liveness checks decide the job's fate. Network errors
            # (TransportError) propagate so the report is retried.
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
                level = logging.ERROR if exc.tls else logging.WARNING
                log.log(level, "Marketplace unreachable (%s); retrying in %.0fs", exc, backoff)
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
