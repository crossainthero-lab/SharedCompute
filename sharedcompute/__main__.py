"""Command line: `python -m sharedcompute serve | add-user | make-operator`."""
from __future__ import annotations

import argparse
import getpass
import os
import sys

from .config import Settings
from .db import Database

LOOPBACK = {"127.0.0.1", "localhost", "::1"}


class PolicyError(Exception):
    pass


def apply_bind_policy(settings: Settings, host: str, *, tls: bool, behind_proxy: bool,
                      env: dict | None = None) -> list[str]:
    """Tighten settings for anything reachable from other machines. Returns notes to print.

    A server is exposed when it binds a non-loopback address or sits behind a TLS proxy. Exposed servers
    must have TLS (directly or via the proxy); registration closes, the marketplace requires sign-in and
    API docs are hidden, unless the operator explicitly re-enables them through environment variables.
    """
    env = os.environ if env is None else env
    exposed = host not in LOOPBACK or behind_proxy
    if not exposed:
        if tls:
            settings.cookie_secure = True
        return []
    if not (tls or behind_proxy):
        raise PolicyError(
            f"Refusing to serve on {host} without TLS. Pass --tls-cert/--tls-key, or run behind a TLS-terminating "
            "proxy with --behind-tls-proxy (see docs/REMOTE_CONNECTIVITY.md).")
    settings.cookie_secure = True
    notes = []
    if "SHAREDCOMPUTE_REGISTRATION" not in env:
        settings.registration_open = False
        notes.append("Registration is closed; create accounts with `python -m sharedcompute add-user`.")
    if "SHAREDCOMPUTE_PUBLIC_MARKETPLACE" not in env:
        settings.public_marketplace = False
        notes.append("The marketplace requires sign-in.")
    if "SHAREDCOMPUTE_API_DOCS" not in env:
        settings.expose_api_docs = False
    return notes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m sharedcompute")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="Run the marketplace API and web UI")
    serve.add_argument("--host", default="127.0.0.1", help="Bind address (default: loopback only)")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--tls-cert", help="PEM certificate chain; enables HTTPS")
    serve.add_argument("--tls-key", help="PEM private key for --tls-cert")
    serve.add_argument("--behind-tls-proxy", action="store_true",
                       help="A TLS-terminating reverse proxy (Caddy, nginx, tailscale serve) fronts this server")

    add = sub.add_parser("add-user", help="Create an account (works when registration is closed)")
    add.add_argument("email")
    add.add_argument("--name", required=True)
    add.add_argument("--operator", action="store_true")
    add.add_argument("--password-stdin", action="store_true", help="Read the password from standard input")

    op = sub.add_parser("make-operator", help="Grant operator access (platform economics) to an account")
    op.add_argument("email")
    op.add_argument("--revoke", action="store_true")

    args = parser.parse_args(argv)
    settings = Settings.from_env()

    if args.command == "serve":
        if bool(args.tls_cert) != bool(args.tls_key):
            print("--tls-cert and --tls-key must be given together.", file=sys.stderr)
            return 2
        try:
            notes = apply_bind_policy(settings, args.host, tls=bool(args.tls_cert), behind_proxy=args.behind_tls_proxy)
        except PolicyError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        import uvicorn

        from .api import create_app

        scheme = "https" if args.tls_cert else "http"
        print(f"SharedCompute {settings.db_path} -> {scheme}://{args.host}:{args.port}")
        for note in notes:
            print(f"  {note}")
        options = {}
        if args.tls_cert:
            options.update(ssl_certfile=args.tls_cert, ssl_keyfile=args.tls_key)
        if args.behind_tls_proxy:
            options.update(proxy_headers=True,
                           forwarded_allow_ips=os.environ.get("SHAREDCOMPUTE_TRUSTED_PROXIES", "127.0.0.1"))
        uvicorn.run(create_app(settings), host=args.host, port=args.port, log_level="info", **options)
        return 0

    from .errors import DomainError
    from .services import accounts

    db = Database(settings.db_path)
    db.migrate()

    if args.command == "add-user":
        if args.password_stdin:
            password = sys.stdin.readline().rstrip("\n")
        else:
            password = getpass.getpass("Password (10+ characters): ")
            if password != getpass.getpass("Repeat password: "):
                print("Passwords do not match.", file=sys.stderr)
                return 1
        try:
            with db.write() as conn:
                user = accounts.register(conn, settings, args.email, password, args.name)
                if args.operator:
                    accounts.set_operator(conn, args.email)
        except DomainError as exc:
            print(exc.message, file=sys.stderr)
            return 1
        print(f"Created {user['email']}{' (operator)' if args.operator else ''}")
        return 0

    if args.command == "make-operator":
        try:
            with db.write() as conn:
                accounts.set_operator(conn, args.email, not args.revoke)
        except DomainError as exc:
            print(exc.message, file=sys.stderr)
            return 1
        print(f"{'Revoked' if args.revoke else 'Granted'} operator access for {args.email}")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
