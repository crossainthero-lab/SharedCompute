"""Command line: `python -m sharedcompute serve | make-operator EMAIL`."""
from __future__ import annotations

import argparse
import sys

from .config import Settings
from .db import Database


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m sharedcompute")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="Run the marketplace API and web UI")
    serve.add_argument("--host", default="127.0.0.1",
                       help="Bind address. Anything other than localhost exposes an unencrypted server.")
    serve.add_argument("--port", type=int, default=8765)

    op = sub.add_parser("make-operator", help="Grant operator access (platform economics) to an account")
    op.add_argument("email")
    op.add_argument("--revoke", action="store_true")

    args = parser.parse_args(argv)
    settings = Settings.from_env()

    if args.command == "serve":
        import uvicorn

        from .api import create_app

        if args.host not in ("127.0.0.1", "localhost", "::1"):
            print(f"WARNING: binding to {args.host} without TLS. Only do this on a trusted network or "
                  "behind a TLS-terminating reverse proxy with SHAREDCOMPUTE_COOKIE_SECURE=1.", file=sys.stderr)
        print(f"SharedCompute {settings.db_path} -> http://{args.host}:{args.port}")
        uvicorn.run(create_app(settings), host=args.host, port=args.port, log_level="info")
        return 0

    if args.command == "make-operator":
        from .errors import NotFound
        from .services import accounts

        db = Database(settings.db_path)
        db.migrate()
        try:
            with db.write() as conn:
                accounts.set_operator(conn, args.email, not args.revoke)
        except NotFound as exc:
            print(exc.message, file=sys.stderr)
            return 1
        print(f"{'Revoked' if args.revoke else 'Granted'} operator access for {args.email}")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
