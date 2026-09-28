"""
Auth API — POST /api/auth/login, GET /api/auth/me, GET /api/auth/audit,
GET /api/auth/notifications

Reads across the rest of the application stay open; these endpoints exist so a
mutating action has an accountable owner and an audit trail.
"""
import logging
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.models.auth import AuditLog, User
from app.schemas import (
    AuditEntryOut,
    CurrentUserOut,
    LoginRequest,
    LoginResponse,
    NotificationOut,
)
from app.services.auth_service import (
    ROLE_PERMISSIONS,
    burn_password_work,
    create_token,
    get_current_user,
    record_audit,
    require_authenticated,
    verify_password,
)
from app.security import client_key, login_limiter, redact_url
from app.services.notification_service import recent_notifications

logger = logging.getLogger(__name__)
settings = get_settings()

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login", response_model=LoginResponse)
async def login(
    payload: LoginRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Exchange credentials for a signed token.

    Throttled per client address. Unthrottled, a 3-account demo with a published
    default password is trivially guessable, and each attempt costs 120k PBKDF2
    rounds, which makes the endpoint a CPU-exhaustion lever as well.
    """
    throttle_key = f"login:{client_key(request)}"
    if settings.rate_limit_enabled:
        retry_after = login_limiter().check(throttle_key)
        if retry_after is not None:
            raise HTTPException(
                status_code=429,
                detail="TOO_MANY_LOGIN_ATTEMPTS",
                headers={"Retry-After": str(int(retry_after))},
            )

    user = (
        await db.execute(select(User).where(User.username == payload.username))
    ).scalar_one_or_none()

    # Same error whether the user is unknown, inactive or the password is wrong,
    # so the endpoint cannot be used to enumerate valid usernames. The dummy hash
    # keeps the cost the same too: without it, an absent username returns in
    # microseconds and the identical error message is defeated by a stopwatch.
    if user is None:
        burn_password_work(payload.password)
        raise HTTPException(status_code=401, detail="INVALID_CREDENTIALS")

    if not user.is_active or not verify_password(
        payload.password, user.password_salt, user.password_hash
    ):
        raise HTTPException(status_code=401, detail="INVALID_CREDENTIALS")

    # Only successful logins clear the budget, so a valid session is never blocked
    # by someone else's failures for long while guessing stays expensive.
    login_limiter().reset(throttle_key)

    await record_audit(
        db, {"sub": user.username, "role": user.role}, "LOGIN", "user", user.id
    )
    await db.commit()

    return LoginResponse(
        token=create_token(user.username, user.role),
        username=user.username,
        role=user.role,
        full_name=user.full_name,
        permissions=sorted(ROLE_PERMISSIONS.get(user.role, set())),
    )


@router.get("/me", response_model=CurrentUserOut)
async def me(request: Request):
    """Report the caller's identity and capabilities.

    Returns 200 with `authenticated: false` rather than 401, so the UI can decide
    what to render without treating "not logged in" as an error.
    """
    user = await get_current_user(request)
    if user is None:
        return CurrentUserOut(auth_enabled=settings.auth_enabled, authenticated=False)

    role = user.get("role", "viewer")
    return CurrentUserOut(
        username=user.get("sub"),
        role=role,
        permissions=sorted(ROLE_PERMISSIONS.get(role, set())),
        auth_enabled=settings.auth_enabled,
        authenticated=True,
    )


@router.get("/audit", response_model=List[AuditEntryOut])
async def audit_trail(
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
    _actor: dict = Depends(require_authenticated),
):
    """Recent audit entries, newest first.

    Authenticated, unlike the dashboard reads: this enumerates operator usernames,
    their roles and what they changed, which is reconnaissance for an attacker and
    has no place on an open wall display.
    """
    limit = max(1, min(limit, 200))
    rows = (
        await db.execute(select(AuditLog).order_by(AuditLog.timestamp.desc()).limit(limit))
    ).scalars().all()
    return [AuditEntryOut.model_validate(r) for r in rows]


@router.get("/notifications", response_model=List[NotificationOut])
async def notifications(
    limit: int = 20,
    db: AsyncSession = Depends(get_db),
    _actor: dict = Depends(require_authenticated),
):
    """Notification delivery attempts, so delivery is verifiable.

    The target is redacted to scheme and host. A Slack or Teams incoming-webhook
    URL is a bearer credential in its path, so returning it verbatim published the
    ability to post into that channel.
    """
    rows = await recent_notifications(db, max(1, min(limit, 100)))
    out = []
    for row in rows:
        entry = NotificationOut.model_validate(row)
        out.append(entry.model_copy(update={"target": redact_url(entry.target)}))
    return out
