"""Minimal JSON client for the marketplace's agent API (urllib, no dependencies).

Every connection is outbound from the provider. Remote marketplaces must use HTTPS with certificate
verification; plain HTTP is only accepted for loopback addresses (see `require_secure_url`).
"""
from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
from typing import Optional
from urllib.parse import urlparse

from . import __version__

LOOPBACK = {"127.0.0.1", "localhost", "::1"}


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"{status} {code}: {message}")
        self.status, self.code, self.message = status, code, message


class TransportError(Exception):
    """The marketplace could not be reached. Callers retry with backoff."""

    def __init__(self, message: str, tls: bool = False):
        super().__init__(message)
        self.tls = tls


class InsecureServerError(ValueError):
    pass


def require_secure_url(server: str) -> None:
    """Refuse to send the node token anywhere but HTTPS or this machine."""
    url = urlparse(server)
    if url.scheme not in ("http", "https") or not url.hostname:
        raise InsecureServerError(f"Not a valid marketplace URL: {server!r}")
    if url.scheme == "http" and url.hostname not in LOOPBACK:
        raise InsecureServerError(
            f"Refusing plain HTTP to {url.hostname}: the node token would cross the network unencrypted. "
            "Use an https:// URL (see docs/REMOTE_CONNECTIVITY.md).")


def tls_context(ca_file: Optional[str] = None) -> ssl.SSLContext:
    """System trust store, or a private CA / self-signed certificate when `ca_file` is given."""
    ctx = ssl.create_default_context(cafile=ca_file)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


class MarketplaceClient:
    def __init__(self, server: str, token: str, timeout: float = 15.0, ca_file: Optional[str] = None):
        require_secure_url(server)
        self.base = server.rstrip("/") + "/api/agent/v1"
        self.token = token
        self.timeout = timeout
        self.context = tls_context(ca_file) if server.startswith("https://") else None

    def post(self, path: str, body: Optional[dict] = None) -> dict:
        req = urllib.request.Request(
            self.base + path, data=json.dumps(body or {}).encode(), method="POST",
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json",
                     "User-Agent": f"sharedcompute-agent/{__version__}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self.context) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            try:
                err = json.loads(exc.read()).get("error", {})
            except (ValueError, AttributeError):
                err = {}
            raise ApiError(exc.code, err.get("code", "http_error"), err.get("message", exc.reason)) from None
        except urllib.error.URLError as exc:
            tls = isinstance(exc.reason, ssl.SSLError)
            hint = " (TLS verification failed: check the URL's hostname and --ca-file)" if tls else ""
            raise TransportError(f"{exc.reason}{hint}", tls=tls) from None
        except ssl.SSLError as exc:
            raise TransportError(f"{exc} (TLS error)", tls=True) from None
        except (TimeoutError, ConnectionError, OSError) as exc:
            raise TransportError(str(exc)) from None

    def hello(self, body: dict) -> dict:
        return self.post("/hello", body)

    def heartbeat(self, body: dict) -> dict:
        return self.post("/heartbeat", body)

    def claim(self) -> Optional[dict]:
        return self.post("/jobs/claim").get("job")

    def complete(self, job_id: int, output: dict, runtime_seconds: float) -> dict:
        return self.post(f"/jobs/{job_id}/complete", {"output": output, "runtime_seconds": runtime_seconds})

    def fail(self, job_id: int, reason: str, retryable: bool, code: Optional[str] = None) -> dict:
        body = {"reason": reason[:300], "retryable": retryable}
        if code:
            body["code"] = code
        return self.post(f"/jobs/{job_id}/fail", body)
