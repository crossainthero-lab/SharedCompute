"""User accounts and browser/API sessions."""
from __future__ import annotations

import re
import sqlite3

from ..config import Settings
from ..errors import Conflict, InvalidInput, NotFound, Unauthorized
from ..security import SESSION_PREFIX, hash_password, new_token, token_hash, verify_password
from ..validation import text

EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,}$")
SAMPLE_NETWORK_EMAIL = "sample-network@sharedcompute.invalid"


def public_user(row: sqlite3.Row) -> dict:
    return {"id": row["id"], "email": row["email"], "display_name": row["display_name"],
            "is_operator": bool(row["is_operator"])}


def register(conn: sqlite3.Connection, settings: Settings, email: object, password: object,
             display_name: object) -> dict:
    if not isinstance(email, str) or not EMAIL_RE.match(email.strip()):
        raise InvalidInput("Enter a valid email address", field="email")
    email = email.strip().lower()
    if not isinstance(password, str) or not 10 <= len(password) <= 256:
        raise InvalidInput("Password must be 10–256 characters", field="password")
    name = text(display_name, "display_name", "Display name", min_len=2, max_len=60)
    if conn.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
        raise Conflict("An account with this email already exists", field="email")
    user_id = conn.execute(
        "INSERT INTO users(email, display_name, password_hash, created_at) VALUES (?, ?, ?, ?)",
        (email, name, hash_password(password, settings.password_iterations), settings.now()),
    ).lastrowid
    return public_user(conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone())


def authenticate(conn: sqlite3.Connection, email: object, password: object) -> dict:
    if not isinstance(email, str) or not isinstance(password, str):
        raise Unauthorized("Email or password is incorrect")
    row = conn.execute("SELECT * FROM users WHERE email = ? AND is_system = 0",
                       (email.strip().lower(),)).fetchone()
    if row is None or not verify_password(password, row["password_hash"]):
        raise Unauthorized("Email or password is incorrect")
    return public_user(row)


def create_session(conn: sqlite3.Connection, settings: Settings, user_id: int) -> str:
    token = new_token(SESSION_PREFIX)
    now = settings.now()
    conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
    conn.execute("INSERT INTO sessions(token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                 (token_hash(token), user_id, now, now + settings.session_ttl_seconds))
    return token


def resolve_session(conn: sqlite3.Connection, settings: Settings, token: str | None) -> dict | None:
    if not token or not token.startswith(SESSION_PREFIX):
        return None
    row = conn.execute(
        """SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id
           WHERE s.token_hash = ? AND s.expires_at > ?""",
        (token_hash(token), settings.now()),
    ).fetchone()
    return public_user(row) if row else None


def revoke_session(conn: sqlite3.Connection, token: str) -> None:
    conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash(token),))


def set_operator(conn: sqlite3.Connection, email: str, value: bool = True) -> None:
    cur = conn.execute("UPDATE users SET is_operator = ? WHERE email = ? AND is_system = 0",
                       (int(value), email.strip().lower()))
    if cur.rowcount == 0:
        raise NotFound(f"No account with email {email}")


def ensure_system_user(conn: sqlite3.Connection, settings: Settings, email: str, name: str) -> int:
    row = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
    if row:
        return row["id"]
    return conn.execute(
        "INSERT INTO users(email, display_name, password_hash, is_system, created_at) VALUES (?, ?, NULL, 1, ?)",
        (email, name, settings.now()),
    ).lastrowid
