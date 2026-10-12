"""Command line: `python -m sharedcompute_agent detect | run`."""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
from pathlib import Path
from urllib.parse import urlparse

from . import gpu, workloads
from .agent import Agent
from .client import ApiError, MarketplaceClient


def _token(args) -> str | None:
    # Never accept the token as a CLI argument: it would be visible in the process list.
    if args.token_file:
        return Path(args.token_file).expanduser().read_text().strip()
    return os.environ.get("SHAREDCOMPUTE_NODE_TOKEN")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m sharedcompute_agent")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("detect", help="Print detected GPUs and supported workloads, then exit")
    run = sub.add_parser("run", help="Connect to the marketplace and serve jobs")
    run.add_argument("--server", default=os.environ.get("SHAREDCOMPUTE_SERVER", "http://127.0.0.1:8765"))
    run.add_argument("--token-file", help="File containing the node token (default: $SHAREDCOMPUTE_NODE_TOKEN)")
    run.add_argument("--max-jobs", type=int, help="Exit after this many jobs (useful for testing)")
    run.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    if args.command == "detect":
        print(json.dumps({"gpus": [g.to_dict() for g in gpu.detect()], "metrics": gpu.metrics(),
                          "capabilities": workloads.capabilities()}, indent=2))
        return 0

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    token = _token(args)
    if not token:
        print("No node token. Set SHAREDCOMPUTE_NODE_TOKEN or pass --token-file.", file=sys.stderr)
        return 2
    url = urlparse(args.server)
    if url.scheme != "https" and url.hostname not in ("127.0.0.1", "localhost", "::1"):
        print("WARNING: talking to a remote marketplace over plain HTTP exposes the node token.",
              file=sys.stderr)
    agent = Agent(MarketplaceClient(args.server, token), max_jobs=args.max_jobs)
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
