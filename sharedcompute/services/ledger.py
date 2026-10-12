"""Double-entry ledger of simulated credits.

Accounts per user: ``available`` (spendable), ``held`` (reserved for in-flight jobs) and
``earnings`` (provider income). Platform accounts: ``platform:revenue`` and
``platform:demo_funding`` (the counter-account for simulated top-ups, so it goes negative).

No real money exists in this system. Entries map one-to-one onto what a payment
processor or stablecoin settlement would later need to reconcile against.
"""
from __future__ import annotations

import sqlite3

from ..errors import InsufficientFunds, InvalidInput

PLATFORM_REVENUE = "platform:revenue"
DEMO_FUNDING = "platform:demo_funding"

MAX_TOPUP_CENTS = 1_000_00
MAX_DEMO_BALANCE_CENTS = 10_000_00


def _code(user_id: int, kind: str) -> str:
    return f"user:{user_id}:{kind}"


def account_id(conn: sqlite3.Connection, code: str, kind: str, user_id: int | None = None) -> int:
    row = conn.execute("SELECT id FROM ledger_accounts WHERE code = ?", (code,)).fetchone()
    if row:
        return row["id"]
    return conn.execute(
        "INSERT INTO ledger_accounts(code, kind, user_id) VALUES (?, ?, ?)", (code, kind, user_id)
    ).lastrowid


def user_account(conn: sqlite3.Connection, user_id: int, kind: str) -> int:
    return account_id(conn, _code(user_id, kind), kind, user_id)


def post(conn: sqlite3.Connection, *, kind: str, postings: list[tuple[int, int]], now: float,
         job_id: int | None = None, user_id: int | None = None, memo: str | None = None) -> int:
    postings = [(acct, amt) for acct, amt in postings if amt != 0]
    if sum(amt for _, amt in postings) != 0:
        raise AssertionError(f"Unbalanced ledger entry {kind}: {postings}")
    entry_id = conn.execute(
        "INSERT INTO ledger_entries(kind, job_id, user_id, memo, created_at) VALUES (?, ?, ?, ?, ?)",
        (kind, job_id, user_id, memo, now),
    ).lastrowid
    conn.executemany(
        "INSERT INTO ledger_postings(entry_id, account_id, amount_cents) VALUES (?, ?, ?)",
        [(entry_id, acct, amt) for acct, amt in postings],
    )
    return entry_id


def balance(conn: sqlite3.Connection, code: str) -> int:
    row = conn.execute(
        """SELECT COALESCE(SUM(p.amount_cents), 0) AS total FROM ledger_postings p
           JOIN ledger_accounts a ON a.id = p.account_id WHERE a.code = ?""",
        (code,),
    ).fetchone()
    return row["total"]


def user_balances(conn: sqlite3.Connection, user_id: int) -> dict:
    return {
        "available_cents": balance(conn, _code(user_id, "available")),
        "held_cents": balance(conn, _code(user_id, "held")),
        "earnings_cents": balance(conn, _code(user_id, "earnings")),
    }


def demo_topup(conn: sqlite3.Connection, user_id: int, cents: int, now: float) -> None:
    if not 1 <= cents <= MAX_TOPUP_CENTS:
        raise InvalidInput("Demo top-ups must be between $0.01 and $1,000.00", field="amount_usd")
    available = balance(conn, _code(user_id, "available"))
    if available + cents > MAX_DEMO_BALANCE_CENTS:
        raise InvalidInput("Demo balances are capped at $10,000.00", field="amount_usd")
    post(conn, kind="demo_topup", now=now, user_id=user_id, memo="Simulated credits (no money moved)",
         postings=[(account_id(conn, DEMO_FUNDING, "demo_funding"), -cents),
                   (user_account(conn, user_id, "available"), cents)])


def hold(conn: sqlite3.Connection, user_id: int, job_id: int, cents: int, now: float) -> None:
    available = balance(conn, _code(user_id, "available"))
    if available < cents:
        raise InsufficientFunds(
            f"This job needs ${cents / 100:,.2f} held from your balance; "
            f"${available / 100:,.2f} is available."
        )
    post(conn, kind="hold", now=now, user_id=user_id, job_id=job_id, memo="Budget held for job",
         postings=[(user_account(conn, user_id, "available"), -cents),
                   (user_account(conn, user_id, "held"), cents)])


def release(conn: sqlite3.Connection, user_id: int, job_id: int, cents: int, now: float, memo: str) -> None:
    post(conn, kind="release", now=now, user_id=user_id, job_id=job_id, memo=memo,
         postings=[(user_account(conn, user_id, "held"), -cents),
                   (user_account(conn, user_id, "available"), cents)])


def charge(conn: sqlite3.Connection, *, customer_id: int, provider_id: int, job_id: int, held_cents: int,
           cost_cents: int, provider_cents: int, platform_cents: int, now: float) -> None:
    """Settle a completed job: consume the hold, pay provider and platform, refund the rest."""
    assert provider_cents + platform_cents == cost_cents <= held_cents
    post(conn, kind="charge", now=now, user_id=customer_id, job_id=job_id, memo="Completed job settled",
         postings=[(user_account(conn, customer_id, "held"), -held_cents),
                   (user_account(conn, customer_id, "available"), held_cents - cost_cents),
                   (user_account(conn, provider_id, "earnings"), provider_cents),
                   (account_id(conn, PLATFORM_REVENUE, "revenue"), platform_cents)])
