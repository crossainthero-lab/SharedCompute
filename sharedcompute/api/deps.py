"""Request-scoped helpers: settings, database, and the authenticated caller."""
from __future__ import annotations

from fastapi import Request

from ..config import Settings
from ..db import Database
from ..errors import Forbidden, Unauthorized
from ..services import accounts

SESSION_COOKIE = "sc_session"


def settings(request: Request) -> Settings:
    return request.app.state.settings


def db(request: Request) -> Database:
    return request.app.state.db


def bearer(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return None


def session_token(request: Request) -> str | None:
    return bearer(request) or request.cookies.get(SESSION_COOKIE)


def optional_user(request: Request) -> dict | None:
    with db(request).read() as conn:
        return accounts.resolve_session(conn, settings(request), session_token(request))


def current_user(request: Request) -> dict:
    user = optional_user(request)
    if user is None:
        raise Unauthorized("Sign in to continue")
    return user


def operator(request: Request) -> dict:
    user = current_user(request)
    if not user["is_operator"]:
        raise Forbidden("Operator access required")
    return user
