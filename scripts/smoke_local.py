"""End-to-end smoke test on ONE machine: a throwaway marketplace plus the real provider agent.

    .venv/bin/python scripts/smoke_local.py               # Test 1: CPU job (any machine, no GPU)
    .venv/bin/python scripts/smoke_local.py --gpu 0       # also a real CUDA job on GPU 0 (NVIDIA Linux)

It never touches your real database, .env or agent config: everything lives in a temporary directory
and the server binds a random loopback port. The GPU is used only if you name it with --gpu, and a GPU
that drives a display is refused unless you also pass --allow-display-gpu.

Exit code 0 = PASS. A JSON report with the evidence is written (and its path printed).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
CSRF = {"X-Requested-With": "SharedCompute"}
PASSWORD = "smoke-test-password-1"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait(predicate, timeout: float, what: str):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.25)
    raise TimeoutError(f"timed out waiting for {what}")


class Smoke:
    def __init__(self, args):
        self.args = args
        self.tmp = Path(tempfile.mkdtemp(prefix="sharedcompute-smoke-"))
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.env = {**os.environ,
                    "SHAREDCOMPUTE_DB": str(self.tmp / "smoke.sqlite3"),
                    "SHAREDCOMPUTE_ENV_FILE": str(self.tmp / "no.env"),        # ignore the repo's .env
                    "SHAREDCOMPUTE_AGENT_ENV": str(self.tmp / "no-agent.env"),  # ignore ~/.config agent.env
                    "SHAREDCOMPUTE_REGISTRATION": "open", "SHAREDCOMPUTE_SIMULATION_SECONDS": "2"}
        for key in ("SHAREDCOMPUTE_GPUS", "SHAREDCOMPUTE_OFFER_CPU", "SHAREDCOMPUTE_NODE_TOKEN", "SHAREDCOMPUTE_TOKEN_FILE"):
            self.env.pop(key, None)
        self.procs: list[subprocess.Popen] = []
        self.report = {"started": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "checks": [], "jobs": {}}

    def check(self, name: str, ok: bool, detail: object = "") -> bool:
        self.report["checks"].append({"check": name, "ok": bool(ok), "detail": detail})
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f": {detail}" if detail != "" else ""))
        return bool(ok)

    def spawn(self, args: list[str], log: str) -> subprocess.Popen:
        out = open(self.tmp / log, "w")
        proc = subprocess.Popen([sys.executable, *args], cwd=ROOT, env=self.env, stdout=out, stderr=subprocess.STDOUT)
        self.procs.append(proc)
        return proc

    def cli(self, *args: str, stdin: str | None = None) -> None:
        subprocess.run([sys.executable, "-m", "sharedcompute", *args], cwd=ROOT, env=self.env, input=stdin,
                       text=True, check=True, capture_output=True)

    def run(self) -> bool:
        print(f"Marketplace: {self.base}  (temporary data in {self.tmp})")
        self.spawn(["-m", "sharedcompute", "serve", "--port", str(self.port)], "server.log")
        wait(lambda: self._get("/api/health"), 30, "the marketplace to start")

        provider = httpx.Client(base_url=self.base, headers=CSRF, timeout=30)
        customer = httpx.Client(base_url=self.base, headers=CSRF, timeout=30)
        provider.post("/api/v1/auth/register", json={"email": "provider@smoke.test", "password": PASSWORD,
                                                     "display_name": "Smoke provider"}).raise_for_status()
        customer.post("/api/v1/auth/register", json={"email": "customer@smoke.test", "password": PASSWORD,
                                                     "display_name": "Smoke customer"}).raise_for_status()
        customer.post("/api/v1/wallet/demo-topup", json={"amount_usd": "10.00"}).raise_for_status()
        self.cli("make-operator", "customer@smoke.test")
        token = provider.post("/api/v1/provider/nodes", json={"kind": "agent", "name": "Smoke node", "region": "au-syd",
                                                              "hourly_usd": "0.50"}).json()["agent_token"]
        token_file = self.tmp / "node.token"
        token_file.write_text(token)
        token_file.chmod(0o600)

        jobs = [("cpu", {"name": "Smoke CPU check", "workload": "agent.hashchain", "min_vram_gb": 0})]
        agent_args = ["-m", "sharedcompute_agent", "run", "--server", self.base, "--token-file", str(token_file),
                      "--offer-cpu", "--max-jobs", "0"]
        if self.args.gpu is not None:
            agent_args += ["--gpu", self.args.gpu]
            if self.args.allow_display_gpu:
                agent_args.append("--allow-display-gpu")
            jobs.append(("gpu", {"name": "Smoke CUDA matmul", "workload": "agent.cuda_matmul", "min_vram_gb": 0}))
        agent_args[agent_args.index("--max-jobs") + 1] = str(len(jobs))
        agent = self.spawn(agent_args, "agent.log")

        try:
            node = wait(lambda: (n := provider.get("/api/v1/provider/nodes").json()["nodes"]) and n[0]["online"] and n[0],
                        30, "the agent to register")
        except TimeoutError:
            self.check("agent registered", False, (self.tmp / "agent.log").read_text()[-800:])
            return False
        offered = [d for d in node["gpus"] if d["offered"]]
        self.check("agent registered and offered only what was chosen",
                   {d["device_type"] for d in offered} <= {"cpu", "gpu"} and any(d["device_type"] == "cpu" for d in offered),
                   ", ".join(f"{d['device_type']}:{d['model']}" for d in offered))
        for d in node["gpus"]:
            if d["device_type"] == "gpu" and not d["offered"]:
                print(f"         GPU {d['slot']} {d['model']} not offered: {d['offer_note']}")
        if self.args.gpu is not None and not any(d["device_type"] == "gpu" for d in offered):
            self.check("requested GPU offered", False, "see the notes above; run `python -m sharedcompute_agent diagnose`")
            return False

        ok = True
        for kind, body in jobs:
            r = customer.post("/api/v1/jobs", json={**body, "est_minutes": 2, "max_budget_usd": "1.00", "region": "any"})
            r.raise_for_status()
            job_id = r.json()["job"]["id"]
            try:
                job = wait(lambda: (j := customer.get(f"/api/v1/jobs/{job_id}").json()["job"])["status"]
                           in ("completed", "failed") and j, 120, f"{kind} job #{job_id}")
            except TimeoutError:
                job = customer.get(f"/api/v1/jobs/{job_id}").json()["job"]
            self.report["jobs"][kind] = job
            expected = "cpu_verified" if kind == "cpu" else "gpu_attested"
            level = (job.get("evidence") or {}).get("level")
            ok &= self.check(f"{kind.upper()} job #{job_id} completed with {expected}",
                             job["status"] == "completed" and job["verified"] and level == expected,
                             f"status={job['status']} evidence={level} executed_on={job['executed_on']}"
                             + (f" reason={job['failure_reason']}" if job["failure_reason"] else ""))
            if job["status"] == "completed":
                ok &= self.check(f"{kind.upper()} job split adds up", job["provider_cents"] + job["platform_cents"] == job["cost_cents"],
                                 f"charged {job['cost_cents']}¢ = provider {job['provider_cents']}¢ + fee {job['platform_cents']}¢")
                if kind == "gpu":
                    ev = job["evidence"]
                    print(f"         device={job['result']['output']['device']} kernel_ms={ev['timing']['kernel_ms']} "
                          f"observed={ev['timing']['server_observed_seconds']}s")

        econ = customer.get("/api/v1/economics").json()
        spent = sum(j["cost_cents"] or 0 for j in self.report["jobs"].values())
        earned = sum(j["provider_cents"] or 0 for j in self.report["jobs"].values())
        balances = econ["personal"]["balances"]
        ok &= self.check("customer balance reconciles", balances["available_cents"] == 1000 - spent and balances["held_cents"] == 0,
                         f"available {balances['available_cents']}¢ after spending {spent}¢; held {balances['held_cents']}¢")
        ok &= self.check("provider earnings reconcile",
                         provider.get("/api/v1/me").json()["balances"]["earnings_cents"] == earned, f"{earned}¢")
        ok &= self.check("platform ledger balanced", econ["platform"]["ledger"]["balanced"],
                         f"revenue {econ['platform']['ledger']['platform_revenue_cents']}¢")
        try:
            agent.wait(timeout=30)
            self.check("agent exited after its jobs", agent.returncode == 0, f"exit {agent.returncode}")
        except subprocess.TimeoutExpired:
            ok &= self.check("agent exited after its jobs", False)
        return ok

    def _get(self, path: str):
        try:
            return httpx.get(self.base + path, timeout=2).status_code == 200
        except httpx.HTTPError:
            return False

    def cleanup(self, passed: bool) -> Path:
        for proc in reversed(self.procs):
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
        self.report["passed"] = passed
        self.report["platform"] = sys.platform
        report = Path(self.args.report) if self.args.report else Path(tempfile.gettempdir()) / f"sharedcompute-smoke-{int(time.time())}.json"
        report.write_text(json.dumps(self.report, indent=2, default=str))
        if self.args.keep or not passed:
            print(f"Logs kept in {self.tmp}")
        else:
            shutil.rmtree(self.tmp, ignore_errors=True)
        return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--gpu", metavar="INDEX|UUID", help="Also run a real CUDA job on this GPU (explicit choice)")
    parser.add_argument("--allow-display-gpu", action="store_true")
    parser.add_argument("--report", help="Where to write the JSON evidence report")
    parser.add_argument("--keep", action="store_true", help="Keep the temporary data and logs")
    smoke = Smoke(parser.parse_args())
    passed = False
    try:
        passed = smoke.run()
    except Exception as exc:  # report and keep logs rather than a bare traceback
        smoke.check("smoke test ran", False, f"{type(exc).__name__}: {exc}")
    finally:
        report = smoke.cleanup(passed)
    print(f"\n{'PASS' if passed else 'FAIL'} · evidence: {report}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
