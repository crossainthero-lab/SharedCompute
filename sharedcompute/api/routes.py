"""User-facing API: auth, marketplace, customer jobs, provider hardware, wallet and economics."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response

from .. import __version__, regions, workloads
from ..errors import Forbidden, RateLimited
from ..security import RateLimiter
from ..services import accounts, economics, jobs, ledger, nodes, scheduler
from .. import validation as v
from . import deps
from .schemas import GpuPatch, JobIn, LoginIn, NodeIn, NodePatch, QuoteIn, RegisterIn, TopupIn

router = APIRouter(prefix="/api/v1")


def _limiter(request: Request) -> RateLimiter:
    app = request.app
    if not hasattr(app.state, "auth_limiter"):
        app.state.auth_limiter = RateLimiter(deps.settings(request).login_attempts_per_minute)
    return app.state.auth_limiter


def _rate_limit(request: Request, key: str) -> None:
    client = request.client.host if request.client else "unknown"
    if not _limiter(request).allow(f"{client}:{key}"):
        raise RateLimited("Too many attempts. Wait a minute and try again.")


def _set_session(request: Request, response: Response, token: str) -> None:
    s = deps.settings(request)
    response.set_cookie(deps.SESSION_COOKIE, token, max_age=s.session_ttl_seconds, httponly=True,
                        samesite="lax", secure=s.cookie_secure, path="/")


# --- Meta & auth ---------------------------------------------------------------------------

@router.get("/meta", tags=["meta"])
def meta(request: Request):
    s = deps.settings(request)
    return {
        "version": __version__,
        "platform_fee_bps": s.platform_fee_bps,
        "simulation_seconds": s.simulation_seconds,
        "demo_credits_enabled": s.demo_credits_enabled,
        "max_attempts": s.max_attempts,
        "workloads": [w.public() for w in workloads.CATALOGUE.values()],
        "regions": [{"code": c, "label": label} for c, label in regions.REGIONS.items()],
    }


@router.post("/auth/register", tags=["auth"], status_code=201)
def register(body: RegisterIn, request: Request, response: Response):
    _rate_limit(request, "register")
    s = deps.settings(request)
    with deps.db(request).write() as conn:
        user = accounts.register(conn, s, body.email, body.password, body.display_name)
        token = accounts.create_session(conn, s, user["id"])
    _set_session(request, response, token)
    return {"user": user, "token": token}


@router.post("/auth/login", tags=["auth"])
def login(body: LoginIn, request: Request, response: Response):
    _rate_limit(request, f"login:{body.email.strip().lower()}")
    s = deps.settings(request)
    with deps.db(request).write() as conn:
        user = accounts.authenticate(conn, body.email, body.password)
        token = accounts.create_session(conn, s, user["id"])
    _set_session(request, response, token)
    return {"user": user, "token": token}


@router.post("/auth/logout", tags=["auth"])
def logout(request: Request, response: Response):
    token = deps.session_token(request)
    if token:
        with deps.db(request).write() as conn:
            accounts.revoke_session(conn, token)
    response.delete_cookie(deps.SESSION_COOKIE, path="/")
    return {"ok": True}


@router.get("/me", tags=["auth"])
def me(request: Request, user: dict = Depends(deps.current_user)):
    with deps.db(request).read() as conn:
        return {"user": user, "balances": ledger.user_balances(conn, user["id"])}


# --- Marketplace --------------------------------------------------------------------------

@router.get("/marketplace", tags=["marketplace"])
def marketplace(request: Request):
    with deps.db(request).read() as conn:
        return nodes.marketplace(conn, deps.settings(request))


@router.post("/quotes", tags=["marketplace"])
def quote(body: QuoteIn, request: Request):
    user = deps.optional_user(request)
    with deps.db(request).read() as conn:
        return scheduler.preview(conn, deps.settings(request), user["id"] if user else 0, body.model_dump())


# --- Customer jobs ------------------------------------------------------------------------

@router.get("/jobs", tags=["jobs"])
def list_jobs(request: Request, status: str | None = None, user: dict = Depends(deps.current_user)):
    s = deps.settings(request)
    with deps.db(request).read() as conn:
        return {"jobs": jobs.list_owned(conn, s, user["id"], status),
                "counts": jobs.status_counts(conn, user["id"])}


@router.post("/jobs", tags=["jobs"], status_code=201)
def submit_job(body: JobIn, request: Request, user: dict = Depends(deps.current_user)):
    s = deps.settings(request)
    with deps.db(request).write() as conn:
        job_id = jobs.submit(conn, s, user["id"], body.model_dump())
        scheduler.schedule(conn, s)
        return {"job": jobs.serialize(conn, s, jobs.row(conn, job_id), with_events=True)}


@router.get("/jobs/{job_id}", tags=["jobs"])
def get_job(job_id: int, request: Request, user: dict = Depends(deps.current_user)):
    s = deps.settings(request)
    with deps.db(request).read() as conn:
        return {"job": jobs.serialize(conn, s, jobs.owned(conn, user["id"], job_id), with_events=True)}


@router.post("/jobs/{job_id}/cancel", tags=["jobs"])
def cancel_job(job_id: int, request: Request, user: dict = Depends(deps.current_user)):
    s = deps.settings(request)
    with deps.db(request).write() as conn:
        jobs.cancel(conn, s, user["id"], job_id)
        scheduler.schedule(conn, s)
        return {"job": jobs.serialize(conn, s, jobs.row(conn, job_id), with_events=True)}


@router.post("/jobs/{job_id}/demo/complete", tags=["demo controls"])
def demo_complete(job_id: int, request: Request, user: dict = Depends(deps.current_user)):
    """Finish a simulated job now instead of waiting for the simulator timer."""
    s = deps.settings(request)
    with deps.db(request).write() as conn:
        jobs.finish_simulated(conn, s, jobs.simulated_job_for_demo(conn, user, job_id))
        scheduler.schedule(conn, s)
    return {"ok": True}


@router.post("/jobs/{job_id}/demo/fail", tags=["demo controls"])
def demo_fail(job_id: int, request: Request, user: dict = Depends(deps.current_user)):
    """Inject a provider-side failure into a simulated job. The hold is released; nothing is charged."""
    s = deps.settings(request)
    with deps.db(request).write() as conn:
        job = jobs.simulated_job_for_demo(conn, user, job_id)
        jobs.fail(conn, s, job, "Simulated provider failure (demo control)", node_id=job["node_id"])
        scheduler.schedule(conn, s)
    return {"ok": True}


# --- Wallet & economics -------------------------------------------------------------------

@router.post("/wallet/demo-topup", tags=["wallet"])
def demo_topup(body: TopupIn, request: Request, user: dict = Depends(deps.current_user)):
    s = deps.settings(request)
    if not s.demo_credits_enabled:
        raise Forbidden("Demo credits are disabled on this deployment")
    cents = v.usd(body.amount_usd, "amount_usd", "Amount")
    with deps.db(request).write() as conn:
        ledger.demo_topup(conn, user["id"], cents, s.now())
        return {"balances": ledger.user_balances(conn, user["id"])}


@router.get("/economics", tags=["economics"])
def get_economics(request: Request, user: dict = Depends(deps.current_user)):
    s = deps.settings(request)
    with deps.db(request).read() as conn:
        return {"fee_bps": s.platform_fee_bps,
                "personal": economics.personal(conn, s, user["id"]),
                "platform": economics.platform(conn, s) if user["is_operator"] else None}


# --- Provider -----------------------------------------------------------------------------

@router.get("/provider/nodes", tags=["provider"])
def provider_nodes(request: Request, user: dict = Depends(deps.current_user)):
    with deps.db(request).read() as conn:
        return {"nodes": nodes.owned_nodes(conn, deps.settings(request), user["id"])}


@router.post("/provider/nodes", tags=["provider"], status_code=201)
def create_node(body: NodeIn, request: Request, user: dict = Depends(deps.current_user)):
    s = deps.settings(request)
    with deps.db(request).write() as conn:
        created = nodes.create_node(conn, s, user["id"], body.model_dump())
        scheduler.schedule(conn, s)
    # The agent token is only ever returned here (and on rotation); only its hash is stored.
    return {"node_id": created["id"], "agent_token": created["agent_token"]}


@router.patch("/provider/nodes/{node_id}", tags=["provider"])
def patch_node(node_id: int, body: NodePatch, request: Request, user: dict = Depends(deps.current_user)):
    s = deps.settings(request)
    with deps.db(request).write() as conn:
        nodes.update_node(conn, s, user["id"], node_id, body.model_dump(exclude_unset=True))
        scheduler.schedule(conn, s)
    return {"ok": True}


@router.delete("/provider/nodes/{node_id}", tags=["provider"])
def delete_node(node_id: int, request: Request, user: dict = Depends(deps.current_user)):
    with deps.db(request).write() as conn:
        nodes.retire_node(conn, deps.settings(request), user["id"], node_id)
    return {"ok": True}


@router.post("/provider/nodes/{node_id}/token", tags=["provider"])
def rotate_node_token(node_id: int, request: Request, user: dict = Depends(deps.current_user)):
    with deps.db(request).write() as conn:
        return {"agent_token": nodes.rotate_token(conn, user["id"], node_id)}


@router.patch("/provider/gpus/{gpu_id}", tags=["provider"])
def patch_gpu(gpu_id: int, body: GpuPatch, request: Request, user: dict = Depends(deps.current_user)):
    s = deps.settings(request)
    with deps.db(request).write() as conn:
        nodes.update_gpu(conn, s, user["id"], gpu_id, body.model_dump(exclude_unset=True))
        scheduler.schedule(conn, s)
    return {"ok": True}


@router.get("/provider/jobs", tags=["provider"])
def provider_jobs(request: Request, user: dict = Depends(deps.current_user)):
    with deps.db(request).read() as conn:
        return {"jobs": jobs.provider_jobs(conn, user["id"])}
