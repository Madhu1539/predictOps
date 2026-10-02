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
import re
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
# Verification links are emailed, so they outlive a session but must not be
# indefinitely replayable.
VERIFICATION_TTL_SECONDS = 24 * 3600

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

# Deliberately permissive and structural rather than a clever pattern. Email syntax
# is far looser than most regexes assume (RFC 5322 allows quoted locals, plus
# addressing, long TLDs), and rejecting a valid address is a worse failure than
# accepting a malformed one that simply never receives its verification link.
_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$")

MIN_PASSWORD_LENGTH = 10
MAX_PASSWORD_LENGTH = 200
MAX_EMAIL_LENGTH = 254  # RFC 5321 limit on a forward path


def normalise_email(email: str) -> str:
    """Lowercased and trimmed. Addresses are case-insensitive in practice, and
    storing them as typed would let `A@x.com` and `a@x.com` become two accounts."""
    return (email or "").strip().lower()


def validate_email(email: str) -> Optional[str]:
    """Return an error message, or None when the address is acceptable."""
    if not email:
        return "Enter an email address."
    if len(email) > MAX_EMAIL_LENGTH:
        return f"Email address must be {MAX_EMAIL_LENGTH} characters or fewer."
    if not _EMAIL_PATTERN.match(email):
        return "That does not look like an email address."
    return None


def validate_password(password: str) -> Optional[str]:
    """Return an error message, or None when the password is acceptable.

    Length is the requirement that actually matters, so it carries the policy. A
    composition rule ("one capital, one digit, one symbol") pushes people towards
    `Password1!`, which is weaker than a longer passphrase and is what every
    cracking dictionary already contains.

    The upper bound exists because PBKDF2 hashes the input at 120k rounds: an
    unbounded password is a CPU-exhaustion lever on an unauthenticated endpoint.
    """
    if not password:
        return "Choose a password."
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
    if len(password) > MAX_PASSWORD_LENGTH:
        return f"Password must be {MAX_PASSWORD_LENGTH} characters or fewer."
    if password.strip() == "":
        return "Password cannot be only whitespace."
    return None


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
    payload = {
        "sub": username,
        "role": role,
        "kind": "session",
        "exp": int(time.time()) + TOKEN_TTL_SECONDS,
    }
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


def create_verification_token(email: str) -> str:
    """A signed, expiring link token proving the holder received the email.

    Signed with the same secret rather than stored in a table: nothing needs to be
    revoked, the token carries its own expiry, and a verification row would be a
    second source of truth for a fact already recorded on the user. `kind` is
    included and checked so a session token cannot be presented as a verification
    token, or the reverse.
    """
    payload = {
        "sub": normalise_email(email),
        "kind": "verify",
        "exp": int(time.time()) + VERIFICATION_TTL_SECONDS,
    }
    payload_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"{payload_b64}.{_sign(payload_b64)}"


def decode_verification_token(token: str) -> Optional[str]:
    """Return the verified email address, or None when the token is unusable."""
    payload = decode_token(token or "")
    if not payload or payload.get("kind") != "verify":
        return None
    return payload.get("sub")


async def get_current_user(request: Request) -> Optional[dict]:
    """Resolve the caller from the Authorization header, if present.

    Requires `kind == "session"`. Both token types are signed with the same secret,
    so without this check a verification link — which is emailed, and therefore far
    more exposed than a session token — would authenticate as its subject when
    presented as a Bearer credential.
    """
    header = request.headers.get("Authorization") or ""
    if not header.lower().startswith("bearer "):
        return None
    payload = decode_token(header.split(" ", 1)[1].strip())
    if not payload or payload.get("kind") != "session":
        return None
    return payload


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
