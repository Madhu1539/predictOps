"""
Authentication and audit — standard library only.

Deliberately dependency-free: PBKDF2 from `hashlib` for password hashing and an
HMAC-signed token from `hmac`. Adding a JWT library would be more machinery than
this needs, and the project's rule is not to introduce technology it does not
require.

Reads stay open so a demo or a wall display cannot break; only mutating actions
are role-gated.
"""
import base64
import hashlib
import hmac
import json
import logging
import os
import time
from typing import Optional

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.models.auth import AuditLog, User
from app.security import resolve_secret_key

logger = logging.getLogger(__name__)
settings = get_settings()

PBKDF2_ROUNDS = 120_000
TOKEN_TTL_SECONDS = 12 * 3600

# Salt used to burn the same CPU when a username does not exist, so a caller
# cannot tell "no such user" from "wrong password" by timing the response.
_DUMMY_SALT = base64.b64encode(b"predictops-timing-equalisation").decode()

# Role capability model. Planner runs the maintenance plan; technician executes
# it; viewer observes.
ROLE_PERMISSIONS = {
    "planner": {"alert:update", "workorder:create", "workorder:update", "reading:ingest"},
    "technician": {"alert:update", "workorder:update", "reading:ingest"},
    "viewer": set(),
}


def hash_password(password: str, salt: Optional[str] = None) -> tuple[str, str]:
    salt = salt or base64.b64encode(os.urandom(16)).decode()
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt.encode(), PBKDF2_ROUNDS
    )
    return salt, base64.b64encode(digest).decode()


def verify_password(password: str, salt: str, expected_hash: str) -> bool:
    _, candidate = hash_password(password, salt)
    # Constant-time comparison so a wrong password cannot be found by timing.
    return hmac.compare_digest(candidate, expected_hash)


def burn_password_work(password: str) -> None:
    """Do the hashing work for a user that does not exist.

    Without this, an absent username returns in microseconds while a real one
    costs 120k PBKDF2 rounds, which makes the login endpoint a reliable username
    oracle regardless of the response body being identical.
    """
    hash_password(password, _DUMMY_SALT)


def _sign(payload_b64: str) -> str:
    return base64.urlsafe_b64encode(
        hmac.new(
            resolve_secret_key().encode(), payload_b64.encode(), hashlib.sha256
        ).digest()
    ).decode().rstrip("=")


def create_token(username: str, role: str) -> str:
    payload = {"sub": username, "role": role, "exp": int(time.time()) + TOKEN_TTL_SECONDS}
    payload_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"{payload_b64}.{_sign(payload_b64)}"


def decode_token(token: str) -> Optional[dict]:
    """Return the payload if the signature and expiry are valid, else None."""
    try:
        payload_b64, signature = token.split(".", 1)
    except ValueError:
        return None

    if not hmac.compare_digest(_sign(payload_b64), signature):
        return None

    try:
        padding = "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + padding))
    except Exception:
        return None

    if payload.get("exp", 0) < int(time.time()):
        return None
    return payload


async def get_current_user(request: Request) -> Optional[dict]:
    """Resolve the caller from the Authorization header, if present."""
    header = request.headers.get("Authorization") or ""
    if not header.lower().startswith("bearer "):
        return None
    return decode_token(header.split(" ", 1)[1].strip())


def require_permission(permission: str):
    """Dependency factory gating a mutating endpoint on a role capability.

    When `auth_enabled` is false the gate is a no-op, so the stack can still be
    demoed or load-tested without tokens.
    """

    async def dependency(request: Request) -> Optional[dict]:
        if not settings.auth_enabled:
            return None

        user = await get_current_user(request)
        if user is None:
            raise HTTPException(status_code=401, detail="AUTH_REQUIRED")

        allowed = ROLE_PERMISSIONS.get(user.get("role", "viewer"), set())
        if permission not in allowed:
            raise HTTPException(status_code=403, detail="INSUFFICIENT_ROLE")
        return user

    return dependency


async def require_authenticated(request: Request) -> Optional[dict]:
    """Require any valid token, without demanding a specific capability.

    For reads that are not plant telemetry: the audit trail names who did what,
    and the notification log describes the delivery channel. Neither belongs on a
    wall display, so they are gated even though dashboard reads are open. Still a
    no-op when `auth_enabled` is false, matching `require_permission`, so the
    demo behaves as before.
    """
    if not settings.auth_enabled:
        return None

    user = await get_current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="AUTH_REQUIRED")
    return user


async def record_audit(
    db: AsyncSession,
    actor: Optional[dict],
    action: str,
    entity_type: str,
    entity_id: Optional[int] = None,
    details: Optional[str] = None,
) -> None:
    """Append an audit entry. Never raises — losing an audit row must not fail
    the user's action, but it must be logged loudly."""
    try:
        db.add(
            AuditLog(
                actor=(actor or {}).get("sub", "anonymous"),
                role=(actor or {}).get("role"),
                action=action,
                entity_type=entity_type,
                entity_id=entity_id,
                details=details,
            )
        )
        await db.flush()
    except Exception as exc:  # pragma: no cover - defensive
        logger.error(f"Failed to write audit entry {action}: {exc}")
