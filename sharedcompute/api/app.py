"""FastAPI application factory: middleware, error mapping, background ticker and static UI."""
from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..config import Settings
from ..db import Database
from ..errors import DomainError
from ..services import nodes, scheduler
from . import agent_routes, routes

log = logging.getLogger("sharedcompute")
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
MAX_BODY_BYTES = 64 * 1024
CSRF_HEADER = "x-requested-with"
CSRF_VALUE = "SharedCompute"
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
       "font-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


def error_body(code: str, message: str, field: str | None = None) -> dict:
    return {"error": {"code": code, "message": message, "field": field}}


class Ticker:
    """Runs scheduler.tick on an interval in a daemon thread."""

    def __init__(self, db: Database, settings: Settings):
        self.db, self.settings = db, settings
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="sharedcompute-ticker", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.wait(self.settings.tick_interval_seconds):
            try:
                with self.db.write() as conn:
                    scheduler.tick(conn, self.settings)
            except Exception:  # keep ticking; failures are logged, not swallowed silently
                log.exception("Scheduler tick failed")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.validate()
    db = Database(settings.db_path)
    db.migrate()
    if settings.seed_sample_network:
        with db.write() as conn:
            nodes.seed_sample_network(conn, settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        ticker = Ticker(db, settings) if settings.run_background_loop else None
        if ticker:
            ticker.start()
        try:
            yield
        finally:
            if ticker:
                ticker.stop()

    app = FastAPI(title="SharedCompute API", version=__version__, lifespan=lifespan,
                  docs_url="/api/docs", openapi_url="/api/openapi.json", redoc_url=None)
    app.state.settings = settings
    app.state.db = db

    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        length = request.headers.get("content-length")
        if length is not None and (not length.isdigit() or int(length) > MAX_BODY_BYTES):
            return JSONResponse(error_body("payload_too_large", "Request body is too large"), status_code=413)
        # CSRF: browser requests to the user API must carry a custom header, which a cross-site
        # form cannot set and a cross-origin fetch cannot send without a CORS preflight we never allow.
        if (path.startswith("/api/v1/") and request.method not in ("GET", "HEAD", "OPTIONS")
                and request.headers.get(CSRF_HEADER) != CSRF_VALUE):
            return JSONResponse(error_body("csrf", f"Missing {CSRF_HEADER} header"), status_code=403)
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("X-Frame-Options", "DENY")
        if path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        else:
            # Unhashed asset names: always revalidate (cheap 304s via ETag) so updates apply at once.
            response.headers.setdefault("Cache-Control", "no-cache")
        if not path.startswith("/api/docs"):
            response.headers.setdefault("Content-Security-Policy", CSP)
        return response

    @app.exception_handler(DomainError)
    async def domain_error(_: Request, exc: DomainError):
        return JSONResponse(error_body(exc.code, exc.message, exc.field), status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError):
        first = exc.errors()[0] if exc.errors() else {}
        loc = [str(p) for p in first.get("loc", []) if p not in ("body", "query", "path")]
        field = ".".join(loc) or None
        message = first.get("msg", "Invalid request")
        if first.get("type") == "extra_forbidden":
            message = "Unknown field"
        elif first.get("type") == "missing":
            message = "This field is required"
        return JSONResponse(error_body("invalid_input", f"{field}: {message}" if field else message, field),
                            status_code=422)

    @app.get("/api/health", tags=["meta"])
    def health():
        with db.read() as conn:
            conn.execute("SELECT 1").fetchone()
        return {"ok": True, "version": __version__}

    app.include_router(routes.router)
    app.include_router(agent_routes.router)

    @app.api_route("/api/{rest:path}", methods=["GET", "POST", "PATCH", "DELETE", "PUT"], include_in_schema=False)
    def api_not_found(rest: str):
        return JSONResponse(error_body("not_found", "No such API endpoint"), status_code=404)

    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
    return app
