"""Minimal JSON client for the marketplace's agent API (urllib, no dependencies)."""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from . import __version__


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"{status} {code}: {message}")
        self.status, self.code, self.message = status, code, message


class TransportError(Exception):
    """The marketplace could not be reached. Callers retry with backoff."""


class MarketplaceClient:
    def __init__(self, server: str, token: str, timeout: float = 15.0):
        self.base = server.rstrip("/") + "/api/agent/v1"
        self.token = token
        self.timeout = timeout

    def post(self, path: str, body: dict | None = None) -> dict:
        req = urllib.request.Request(
            self.base + path, data=json.dumps(body or {}).encode(), method="POST",
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json",
                     "User-Agent": f"sharedcompute-agent/{__version__}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            try:
                err = json.loads(exc.read()).get("error", {})
            except (ValueError, AttributeError):
                err = {}
            raise ApiError(exc.code, err.get("code", "http_error"), err.get("message", exc.reason)) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            raise TransportError(str(exc)) from None

    def hello(self, body: dict) -> dict:
        return self.post("/hello", body)

    def heartbeat(self, body: dict) -> dict:
        return self.post("/heartbeat", body)

    def claim(self) -> dict | None:
        return self.post("/jobs/claim").get("job")

    def complete(self, job_id: int, output: dict, runtime_seconds: float) -> dict:
        return self.post(f"/jobs/{job_id}/complete", {"output": output, "runtime_seconds": runtime_seconds})

    def fail(self, job_id: int, reason: str, retryable: bool) -> dict:
        return self.post(f"/jobs/{job_id}/fail", {"reason": reason[:300], "retryable": retryable})
