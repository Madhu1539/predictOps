"""
Cross-cutting security primitives.

Kept in one module so the guarantees are auditable in a single place rather than
spread across routes: signing-key resolution, request throttling, a hard ceiling
on request bodies, and outbound-URL validation.

Standard library only, matching `auth_service`'s existing constraint.
"""
import logging
import os
import secrets
import threading
import time
from collections import deque
from functools import lru_cache
from pathlib import Path
from typing import Deque, Dict, Optional
from urllib.parse import urlparse

from fastapi import HTTPException, Request

from app.config import get_settings

logger = logging.getLogger(__name__)

# The literal shipped in config.py and .env.example. Treated as "no key set".
PLACEHOLDER_SECRET = "predictops-dev-secret-change-me"

# Where a generated development key is persisted. Beside the database, inside
# `backend/`, and gitignored. Persisted rather than regenerated per boot so a
# token issued before a restart stays valid through one.
SECRET_KEY_FILE = Path(__file__).resolve().parent.parent / ".secret_key"


# ─── Signing key ──────────────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def resolve_secret_key() -> str:
    """The HMAC key for session tokens.

    An explicitly configured key always wins. Otherwise, in development a random
    key is generated once and persisted; outside development this raises, because
    signing sessions with a key published in the repository is the same as not
    signing them at all.
    """
    settings = get_settings()
    configured = (settings.secret_key or "").strip()

    if configured and configured != PLACEHOLDER_SECRET:
        if len(configured) < 32:
            logger.warning(
                "SECRET_KEY is shorter than 32 characters. Use "
                "`python -c \"import secrets;print(secrets.token_urlsafe(48))\"`."
            )
        return configured

    if not settings.is_development:
        raise RuntimeError(
            "SECRET_KEY is unset or still the shipped placeholder while "
            f"ENVIRONMENT={settings.environment!r}. Session tokens would be "
            "forgeable by anyone who has read this repository. Generate one with "
            "`python -c \"import secrets;print(secrets.token_urlsafe(48))\"` and "
            "set SECRET_KEY before starting."
        )

    return _load_or_create_dev_key()


def _load_or_create_dev_key() -> str:
    """Read the persisted development key, creating it on first use."""
    try:
        existing = SECRET_KEY_FILE.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    except (FileNotFoundError, OSError):
        pass

    generated = secrets.token_urlsafe(48)
    try:
        SECRET_KEY_FILE.write_text(generated, encoding="utf-8")
        # Best effort: POSIX has modes to set, Windows reports none.
        if os.name != "nt":
            os.chmod(SECRET_KEY_FILE, 0o600)
        logger.info(
            "Generated a development signing key at %s. Set SECRET_KEY "
            "explicitly for any real deployment.", SECRET_KEY_FILE,
        )
    except OSError as exc:
        # An unwritable directory must not stop the app; the key then lives for
        # this process only, which invalidates tokens across a restart.
        logger.warning(
            "Could not persist the development signing key (%s). Tokens will not "
            "survive a restart.", exc,
        )
    return generated


def verify_startup_configuration() -> None:
    """Fail fast on configurations that are unsafe to serve.

    Called at startup so a misconfigured deployment stops immediately rather than
    running with forgeable tokens or an open mutation surface.
    """
    settings = get_settings()

    # Resolving here surfaces the placeholder-secret error at boot, not on the
    # first login attempt.
    resolve_secret_key()

    if settings.is_development:
        if not settings.auth_enabled:
            logger.warning(
                "AUTH_ENABLED=false: every mutating endpoint is open to anyone who "
                "can reach this port. Acceptable for a local demo only."
            )
        return

    problems = []
    if not settings.auth_enabled:
        problems.append(
            "AUTH_ENABLED=false leaves every mutating endpoint (ingest, dataset "
            "delete, alert and work-order updates) open to unauthenticated callers"
        )
    if settings.demo_password == "predictops":
        problems.append(
            "DEMO_PASSWORD is still the published default, so the seeded planner "
            "account can be logged into by anyone"
        )
    if settings.demo_mode:
        problems.append("DEMO_MODE=true seeds shared demo accounts")

    if problems:
        raise RuntimeError(
            f"Refusing to start with ENVIRONMENT={settings.environment!r}: "
            + "; ".join(problems)
            + ". Correct these or set ENVIRONMENT=development to acknowledge that "
              "this is a demo instance."
        )


# ─── Throttling ───────────────────────────────────────────────────────────────

class SlidingWindowLimiter:
    """In-process sliding-window counter.

    Deliberately in-process and therefore per-worker: this project runs a single
    uvicorn process, and introducing Redis for a rate limit would be more
    machinery than it needs. The limit is consequently a brake on scripted abuse,
    not a distributed quota, and it resets on restart.
    """

    def __init__(self, limit: int, window_seconds: float) -> None:
        self.limit = max(1, limit)
        self.window = max(1.0, float(window_seconds))
        self._hits: Dict[str, Deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> Optional[float]:
        """Record an attempt. Returns None when allowed, else seconds to wait."""
        now = time.monotonic()
        cutoff = now - self.window
        with self._lock:
            bucket = self._hits.setdefault(key, deque())
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()

            if len(bucket) >= self.limit:
                return max(1.0, round(bucket[0] + self.window - now, 1))

            bucket.append(now)
            # Opportunistic sweep so idle keys cannot accumulate unbounded.
            if len(self._hits) > 2048:
                for stale in [k for k, v in self._hits.items() if not v]:
                    del self._hits[stale]
            return None

    def reset(self, key: Optional[str] = None) -> None:
        """Clear a key's history, e.g. after a successful login."""
        with self._lock:
            if key is None:
                self._hits.clear()
            else:
                self._hits.pop(key, None)


def client_key(request: Request) -> str:
    """Identify the caller for throttling.

    `X-Forwarded-For` is attacker-controlled in general: a client can send any
    value it likes, so trusting the header blindly lets one caller bypass every
    limit by varying it. Trusting the peer address blindly is equally wrong behind
    a reverse proxy, because every request then carries the proxy's address and all
    users share a single bucket — on a platform like Render that means the first
    handful of visitors exhaust the limit for everyone.

    `TRUSTED_PROXY_HOPS` resolves this. It states how many proxies sit in front of
    this process and are known to append to the header:

    * 0 (default, and correct for local use) — trust nothing, use the peer address.
    * N > 0 — take the Nth entry from the *right*. Each trusted proxy appends the
      address it received the request from, so the rightmost N entries were written
      by infrastructure we control and anything further left came from the client
      and is ignored. Render terminates TLS at one proxy, so N=1 there.

    A header too short to satisfy N hops means the request did not arrive through
    the expected chain, so the peer address is used rather than the closest guess.
    """
    peer = request.client.host if request.client else "unknown"

    hops = get_settings().trusted_proxy_hops
    if hops <= 0:
        return peer

    forwarded = request.headers.get("x-forwarded-for", "")
    chain = [part.strip() for part in forwarded.split(",") if part.strip()]
    if len(chain) < hops:
        return peer
    return chain[-hops]


@lru_cache(maxsize=1)
def _llm_limiter() -> SlidingWindowLimiter:
    settings = get_settings()
    return SlidingWindowLimiter(
        settings.llm_request_limit, settings.llm_request_window_seconds
    )


@lru_cache(maxsize=1)
def login_limiter() -> SlidingWindowLimiter:
    settings = get_settings()
    return SlidingWindowLimiter(
        settings.login_attempt_limit, settings.login_attempt_window_seconds
    )


async def limit_llm_requests(request: Request) -> None:
    """Dependency for endpoints that spend model quota or money per call."""
    if not get_settings().rate_limit_enabled:
        return
    retry_after = _llm_limiter().check(f"llm:{client_key(request)}")
    if retry_after is not None:
        raise HTTPException(
            status_code=429,
            detail="RATE_LIMITED",
            headers={"Retry-After": str(int(retry_after))},
        )


# ─── Request bodies ───────────────────────────────────────────────────────────

async def read_capped_body(request: Request) -> bytes:
    """Read the raw body, refusing anything over `max_upload_bytes`.

    `request.body()` buffers the whole payload, so an unbounded read is a
    single-request memory-exhaustion lever. `Content-Length` is checked first as a
    cheap rejection, then the stream is accumulated with the same ceiling because
    a chunked request need not declare a length.
    """
    limit = get_settings().max_upload_bytes

    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > limit:
                raise HTTPException(status_code=413, detail="PAYLOAD_TOO_LARGE")
        except ValueError:
            raise HTTPException(status_code=400, detail="INVALID_CONTENT_LENGTH")

    chunks = bytearray()
    async for chunk in request.stream():
        chunks.extend(chunk)
        if len(chunks) > limit:
            raise HTTPException(status_code=413, detail="PAYLOAD_TOO_LARGE")
    return bytes(chunks)


# ─── Outbound URLs ────────────────────────────────────────────────────────────

def validate_webhook_url(url: str) -> Optional[str]:
    """Return the URL if it is safe to POST to, else None.

    Only http and https: without this check a `file://` or `gopher://` value in
    configuration becomes a request the server makes on someone's behalf. Plain
    http is refused outside development because the payload names machines and
    risk levels.
    """
    candidate = (url or "").strip()
    if not candidate:
        return None

    try:
        parsed = urlparse(candidate)
    except ValueError:
        logger.warning("Notification webhook URL is unparseable; delivery disabled.")
        return None

    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        logger.warning(
            "Notification webhook scheme %r is not allowed; delivery disabled.",
            parsed.scheme or "(none)",
        )
        return None

    if parsed.scheme == "http" and not get_settings().is_development:
        logger.warning(
            "Notification webhook uses plain http outside development; delivery "
            "disabled. Use https."
        )
        return None

    return candidate


def redact_url(url: Optional[str]) -> Optional[str]:
    """Scheme, host and a path shape only.

    Incoming-webhook URLs are bearer credentials: the path segment for Slack or
    Teams is the secret. Echoing one back from an API response hands it over.
    """
    if not url:
        return None
    try:
        parsed = urlparse(url)
    except ValueError:
        return "(redacted)"
    if not parsed.netloc:
        return "(redacted)"
    suffix = "/…" if parsed.path.strip("/") else ""
    return f"{parsed.scheme}://{parsed.netloc}{suffix}"
