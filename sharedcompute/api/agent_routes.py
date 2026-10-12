"""Provider-agent API. Authenticated with a per-node bearer token, never a user session."""
from __future__ import annotations

from fastapi import APIRouter, Request

from ..services import agent_protocol, scheduler
from . import deps
from .schemas import CompleteIn, FailIn, HeartbeatIn, HelloIn

router = APIRouter(prefix="/api/agent/v1", tags=["provider agent"])


def _node(conn, request: Request):
    return agent_protocol.authenticate(conn, deps.bearer(request))


@router.post("/hello")
def hello(body: HelloIn, request: Request):
    s = deps.settings(request)
    with deps.db(request).write() as conn:
        result = agent_protocol.hello(conn, s, _node(conn, request), body.model_dump())
        scheduler.schedule(conn, s)
        return result


@router.post("/heartbeat")
def heartbeat(body: HeartbeatIn, request: Request):
    s = deps.settings(request)
    with deps.db(request).write() as conn:
        return agent_protocol.heartbeat(conn, s, _node(conn, request), body.model_dump())


@router.post("/jobs/claim")
def claim(request: Request):
    s = deps.settings(request)
    with deps.db(request).write() as conn:
        node = _node(conn, request)
        scheduler.schedule(conn, s)
        return agent_protocol.claim(conn, s, node)


@router.post("/jobs/{job_id}/complete")
def complete(job_id: int, body: CompleteIn, request: Request):
    s = deps.settings(request)
    with deps.db(request).write() as conn:
        result = agent_protocol.complete(conn, s, _node(conn, request), job_id, body.model_dump())
        scheduler.schedule(conn, s)
        return result


@router.post("/jobs/{job_id}/fail")
def fail(job_id: int, body: FailIn, request: Request):
    s = deps.settings(request)
    with deps.db(request).write() as conn:
        result = agent_protocol.fail(conn, s, _node(conn, request), job_id, body.model_dump())
        scheduler.schedule(conn, s)
        return result
