"""Command line: `python -m sharedcompute_agent detect | diagnose | run`."""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import stat
import sys
from pathlib import Path
from typing import List, Optional

from . import gpu, workloads
from .agent import Agent
from .client import ApiError, InsecureServerError, MarketplaceClient
from .diagnose import diagnose, render
from .policy import OfferPolicy


def _token(args) -> Optional[str]:
    # Never accept the token as a CLI argument: it would be visible in the process list.
    if args.token_file:
        path = Path(args.token_file).expanduser()
        if os.name == "posix" and path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            print(f"WARNING: {path} is readable by other users; run: chmod 600 {path}", file=sys.stderr)
        return path.read_text().strip()
    return os.environ.get("SHAREDCOMPUTE_NODE_TOKEN")


def _policy(args) -> OfferPolicy:
    gpus = list(args.gpu or [])
    if not gpus and os.environ.get("SHAREDCOMPUTE_GPUS"):
        gpus = [g for g in os.environ["SHAREDCOMPUTE_GPUS"].split(",") if g.strip()]
    offer_cpu = args.offer_cpu or os.environ.get("SHAREDCOMPUTE_OFFER_CPU", "").lower() in ("1", "true", "yes")
    return OfferPolicy(gpus=tuple(g.strip() for g in gpus), offer_cpu=offer_cpu,
                       allow_display_gpu=args.allow_display_gpu)


def _offer_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--gpu", action="append", metavar="INDEX|UUID",
                   help="Offer this GPU (nvidia-smi index or UUID prefix). Repeatable. Nothing is offered by default.")
    p.add_argument("--offer-cpu", action="store_true", help="Offer this machine's CPU for CPU workloads")
    p.add_argument("--allow-display-gpu", action="store_true",
                   help="Allow offering a GPU that drives a display (off by default to protect your main GPU)")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m sharedcompute_agent")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("detect", help="Print detected devices as JSON, then exit")

    diag = sub.add_parser("diagnose", help="Explain what each device can run; optionally self-test a GPU")
    _offer_flags(diag)
    diag.add_argument("--selftest", action="store_true", help="Run the real GPU kernel on GPUs chosen with --gpu")
    diag.add_argument("--json", action="store_true")

    run = sub.add_parser("run", help="Connect to the marketplace and serve jobs")
    _offer_flags(run)
    run.add_argument("--server", default=os.environ.get("SHAREDCOMPUTE_SERVER", "http://127.0.0.1:8765"))
    run.add_argument("--token-file", help="File containing the node token (default: $SHAREDCOMPUTE_NODE_TOKEN)")
    run.add_argument("--ca-file", default=os.environ.get("SHAREDCOMPUTE_CA_FILE"),
                     help="PEM CA bundle to trust for the marketplace (private CA or self-signed pilot server)")
    run.add_argument("--allow-cpu-fallback", action="store_true",
                     help="If CUDA fails, run GPU workloads on the CPU. Reported as CPU work and never billed as GPU.")
    run.add_argument("--max-jobs", type=int, help="Exit after this many jobs (useful for testing)")
    run.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    if args.command == "detect":
        devices = gpu.detect() + [gpu.cpu_device()]
        print(json.dumps({"devices": [d.to_report() for d in devices], "metrics": gpu.metrics()}, indent=2))
        return 0

    if args.command == "diagnose":
        report = diagnose(_policy(args), selftest=args.selftest)
        print(json.dumps(report, indent=2) if args.json else render(report))
        return 1 if any(not t["ok"] for t in report["selftests"]) else 0

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    token = _token(args)
    if not token:
        print("No node token. Set SHAREDCOMPUTE_NODE_TOKEN or pass --token-file.", file=sys.stderr)
        return 2
    try:
        client = MarketplaceClient(args.server, token, ca_file=args.ca_file)
    except InsecureServerError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    agent = Agent(client, policy=_policy(args), max_jobs=args.max_jobs,
                  options=workloads.RunOptions(allow_cpu_fallback=args.allow_cpu_fallback))
    signal.signal(signal.SIGTERM, lambda *_: agent.stop.set())
    try:
        agent.run()
    except KeyboardInterrupt:
        agent.stop.set()
        agent._shutdown()
    except ApiError:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
