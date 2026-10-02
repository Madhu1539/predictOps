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
    RegisterRequest,
    RegisterResponse,
    VerifyRequest,
    VerifyResponse,
)
from app.services.auth_service import (
    ROLE_PERMISSIONS,
    burn_password_work,
    create_token,
    create_verification_token,
    decode_verification_token,
    get_current_user,
    hash_password,
    normalise_email,
    record_audit,
    require_authenticated,
    validate_email,
    validate_password,
    verify_password,
)
from app.security import client_key, login_limiter, redact_url
from app.services.email_service import send_verification_email, verification_link
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

    identifier = (payload.username or "").strip()
    lowered = normalise_email(identifier)

    # Registered accounts sign in with their email, seeded operators with their
    # username. One query covers both: a registered account's username IS its
    # email, and `email` is stored lowercased so the comparison is case-insensitive
    # for addresses while staying exact for usernames.
    user = (
        await db.execute(
            select(User).where(
                (User.username == identifier) | (User.email == lowered)
            )
        )
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

    # Verification is checked only AFTER the password, and only for accounts that
    # have an email. Checking it first would tell an anonymous caller which
    # addresses are registered without knowing the password. Seeded operator
    # accounts have no email and are unaffected.
    if (
        settings.registration_require_verification
        and user.email
        and not user.email_verified
    ):
        raise HTTPException(status_code=403, detail="EMAIL_NOT_VERIFIED")

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
        email=user.email,
    )


@router.post("/register", response_model=RegisterResponse, status_code=201)
async def register(
    payload: RegisterRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Create an account from an email address and a chosen password.

    Throttled on the same budget as login: registration runs 120k PBKDF2 rounds and
    may send mail, so unthrottled it is both a CPU lever and a way to use this
    service to spray email at third parties.
    """
    if not settings.registration_enabled:
        raise HTTPException(status_code=403, detail="REGISTRATION_DISABLED")

    throttle_key = f"register:{client_key(request)}"
    if settings.rate_limit_enabled:
        retry_after = login_limiter().check(throttle_key)
        if retry_after is not None:
            raise HTTPException(
                status_code=429,
                detail="TOO_MANY_ATTEMPTS",
                headers={"Retry-After": str(int(retry_after))},
            )

    email = normalise_email(payload.email)
    for error in (validate_email(email), validate_password(payload.password)):
        if error:
            raise HTTPException(status_code=400, detail=error)

    existing = (
        await db.execute(
            select(User).where((User.email == email) | (User.username == email))
        )
    ).scalar_one_or_none()

    if existing is not None:
        # Deliberately NOT "that address is already registered". This endpoint is
        # unauthenticated and public, so a distinct response would turn it into an
        # oracle for which addresses hold accounts. The owner of the address learns
        # the real state from their inbox; nobody else learns anything.
        logger.info(f"Registration attempted for an existing account: {email}")
        return RegisterResponse(
            email=email,
            role=settings.registration_role,
            verification_required=settings.registration_require_verification,
            email_sent=False,
            message=(
                "Check your email to finish setting up your account. If you already "
                "have one, sign in instead."
            ),
        )

    salt, digest = hash_password(payload.password)
    verified = not settings.registration_require_verification
    user = User(
        username=email,
        email=email,
        email_verified=verified,
        full_name=(payload.full_name or "").strip() or None,
        role=settings.registration_role,
        password_salt=salt,
        password_hash=digest,
        is_active=True,
    )
    db.add(user)
    await db.flush()

    await record_audit(
        db,
        {"sub": email, "role": user.role},
        "ACCOUNT_REGISTERED",
        "user",
        user.id,
        f"role={user.role} verification_required={not verified}",
    )
    await db.commit()

    if verified:
        return RegisterResponse(
            email=email,
            role=user.role,
            verification_required=False,
            email_sent=False,
            message="Account created. You can sign in now.",
        )

    token = create_verification_token(email)
    sent, detail = send_verification_email(email, token)

    if sent:
        return RegisterResponse(
            email=email,
            role=user.role,
            verification_required=True,
            email_sent=True,
            message="Account created. Check your email for the confirmation link.",
        )

    # No SMTP, or the relay refused. The account exists either way, so the link is
    # surfaced rather than stranding it — and the message says plainly that no email
    # was sent, instead of asking the user to check an inbox that will stay empty.
    return RegisterResponse(
        email=email,
        role=user.role,
        verification_required=True,
        email_sent=False,
        verification_link=verification_link(token),
        message=(
            "Account created, but no email could be sent"
            + (" (email delivery is not configured)." if detail == "smtp_not_configured" else ".")
            + " Use the confirmation link below to finish."
        ),
    )


@router.post("/verify", response_model=VerifyResponse)
async def verify(
    payload: VerifyRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Mark an account's address as confirmed, using the signed link token.

    Idempotent: clicking an already-used link reports success rather than an error,
    because from the person's point of view the address is confirmed either way and
    a failure here reads as "my account is broken".
    """
    email = decode_verification_token(payload.token)
    if not email:
        # Covers a bad signature, an expired link and a session token presented
        # here. One message, because distinguishing them helps only an attacker.
        raise HTTPException(status_code=400, detail="INVALID_OR_EXPIRED_LINK")

    user = (
        await db.execute(select(User).where(User.email == email))
    ).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=400, detail="INVALID_OR_EXPIRED_LINK")

    if user.email_verified:
        return VerifyResponse(
            verified=True, email=email, message="Already confirmed. You can sign in."
        )

    user.email_verified = True
    await record_audit(
        db, {"sub": email, "role": user.role}, "ACCOUNT_VERIFIED", "user", user.id
    )
    await db.commit()

    return VerifyResponse(
        verified=True, email=email, message="Email confirmed. You can sign in now."
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
