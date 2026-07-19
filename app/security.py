"""Optional API-key auth and simple in-process rate limiting (QA P0-2).

When VERIFY_API_KEY is unset, behavior is unchanged (open PoC).
When set, mutating API routes require matching X-API-Key or Authorization: Bearer.
/api/health and static UI stay open so Render health checks and the page load.

Rate limiting (RATE_LIMIT_PER_MINUTE) is best-effort in-process — enough to
blunt casual abuse on a single instance, not a distributed WAF.
"""

from __future__ import annotations

import os
import time
from collections import defaultdict, deque
from threading import Lock
from typing import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

# Paths that never require the optional API key (health + UI assets).
_OPEN_PREFIXES = ("/api/health", "/static", "/")
_PROTECTED_PREFIXES = ("/api/verify", "/api/ingest-form")


def configured_api_key() -> str | None:
    key = os.environ.get("VERIFY_API_KEY", "").strip()
    return key or None


def rate_limit_per_minute() -> int:
    raw = os.environ.get("RATE_LIMIT_PER_MINUTE", "0").strip()
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"


def _extract_key(request: Request) -> str | None:
    header = request.headers.get("x-api-key")
    if header and header.strip():
        return header.strip()
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


class RateLimiter:
    """Sliding 60s window of request timestamps per key."""

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def allow(self, key: str, limit: int) -> bool:
        if limit <= 0:
            return True
        now = time.monotonic()
        window_start = now - 60.0
        with self._lock:
            q = self._hits[key]
            while q and q[0] < window_start:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(now)
            return True


_limiter = RateLimiter()


class ProtectMiddleware(BaseHTTPMiddleware):
    """Auth + rate-limit for verify/ingest API routes only."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        path = request.url.path
        protected = any(path == p or path.startswith(p + "/") or path.startswith(p)
                        for p in _PROTECTED_PREFIXES)
        # /api/verify and /api/verify-batch and /api/ingest-form
        if not (path.startswith("/api/verify") or path.startswith("/api/ingest-form")):
            return await call_next(request)

        limit = rate_limit_per_minute()
        if limit > 0:
            ip = _client_ip(request)
            if not _limiter.allow(ip, limit):
                return JSONResponse(
                    status_code=429,
                    content={
                        "error": {
                            "code": "rate_limited",
                            "message": (
                                "Too many requests from this network. "
                                "Please wait a minute and try again."
                            ),
                        }
                    },
                )

        expected = configured_api_key()
        if expected is not None:
            provided = _extract_key(request)
            if provided != expected:
                return JSONResponse(
                    status_code=401,
                    content={
                        "error": {
                            "code": "unauthorized",
                            "message": (
                                "This service requires an API key. "
                                "Send it as the X-API-Key header."
                            ),
                        }
                    },
                )

        return await call_next(request)
