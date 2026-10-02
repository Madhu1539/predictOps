"""
Self-registration: create an account from an email address and a chosen password,
confirm the address, then sign in with it.

The suite runs against the development database, so every test here cleans up the
accounts it creates. Addresses use a reserved domain (RFC 2606 `example.test`) so a
misconfigured SMTP setting can never deliver mail to a real person.
"""
import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.config import Settings
from app.database import AsyncSessionLocal, init_db
from app.main import app
from app.models.auth import AuditLog, User
from app.services.auth_service import (
    create_token,
    create_verification_token,
    decode_verification_token,
    get_current_user,
    normalise_email,
    validate_email,
    validate_password,
)

GOOD_PASSWORD = "a-long-enough-passphrase"


def fresh_email() -> str:
    return f"operator-{uuid.uuid4().hex[:12]}@example.test"


@pytest_asyncio.fixture
async def client():
    await init_db()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


@pytest_asyncio.fixture
async def registered_emails():
    """Collects addresses created by a test and removes them afterwards."""
    created: list[str] = []
    yield created

    async with AsyncSessionLocal() as db:
        for email in created:
            user = (
                await db.execute(select(User).where(User.email == email))
            ).scalar_one_or_none()
            if user is not None:
                await db.execute(delete(AuditLog).where(AuditLog.entity_id == user.id))
                await db.execute(delete(User).where(User.id == user.id))
        await db.commit()


# ─── Input rules ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "address",
    ["plainstring", "no-at-sign.example.test", "a@b", "a@@b.test", "a b@c.test", ""],
)
def test_malformed_addresses_are_rejected(address):
    assert validate_email(normalise_email(address)) is not None


@pytest.mark.parametrize(
    "address",
    [
        "operator@example.test",
        "first.last@sub.example.test",
        "plus+tag@example.test",
        "UPPER@EXAMPLE.TEST",
    ],
)
def test_real_shaped_addresses_are_accepted(address):
    """Rejecting a valid address is a worse failure than accepting one that simply
    never receives its link, so the rule is structural rather than clever."""
    assert validate_email(normalise_email(address)) is None


def test_addresses_are_stored_case_insensitively():
    """Otherwise A@x.test and a@x.test become two accounts for one mailbox."""
    assert normalise_email("  Operator@Example.TEST ") == "operator@example.test"


def test_short_passwords_are_rejected():
    assert validate_password("short") is not None
    assert validate_password("a" * 9) is not None


def test_length_is_the_policy_not_composition():
    """A long passphrase with no symbols is accepted; composition rules push people
    towards Password1!, which every cracking dictionary already contains."""
    assert validate_password("correct horse battery staple") is None


def test_absurdly_long_passwords_are_rejected():
    """PBKDF2 at 120k rounds makes an unbounded password a CPU-exhaustion lever on
    an unauthenticated endpoint."""
    assert validate_password("x" * 5000) is not None


# ─── Registration ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_registration_creates_an_unverified_account(client, registered_emails):
    email = fresh_email()
    registered_emails.append(email)

    response = await client.post(
        "/api/auth/register",
        json={"email": email, "password": GOOD_PASSWORD, "full_name": "Test Operator"},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["email"] == email
    assert body["role"] == "planner"
    assert body["verification_required"] is True

    async with AsyncSessionLocal() as db:
        user = (
            await db.execute(select(User).where(User.email == email))
        ).scalar_one()
    assert user.username == email
    assert not user.email_verified
    assert user.role == "planner"
    assert user.password_hash and user.password_salt
    assert GOOD_PASSWORD not in user.password_hash


@pytest.mark.asyncio
async def test_registration_without_smtp_returns_the_link(client, registered_emails):
    """With no mail provider the account would otherwise be stranded: created, but
    unable to sign in and with no way to find out why."""
    email = fresh_email()
    registered_emails.append(email)

    body = (
        await client.post(
            "/api/auth/register", json={"email": email, "password": GOOD_PASSWORD}
        )
    ).json()
    assert body["email_sent"] is False
    assert body["verification_link"]
    assert "token=" in body["verification_link"]
    assert "no email could be sent" in body["message"].lower()


@pytest.mark.asyncio
async def test_registering_an_existing_address_does_not_reveal_it(
    client, registered_emails
):
    """The endpoint is public and unauthenticated. A distinct response would make it
    an oracle for which addresses hold accounts."""
    email = fresh_email()
    registered_emails.append(email)

    first = await client.post(
        "/api/auth/register", json={"email": email, "password": GOOD_PASSWORD}
    )
    second = await client.post(
        "/api/auth/register", json={"email": email, "password": "another-long-password"}
    )
    assert first.status_code == second.status_code == 201
    assert "already" not in second.json()["message"].lower().replace(
        "if you already have one", ""
    )

    # And the original password must still be the one that works.
    async with AsyncSessionLocal() as db:
        count = len(
            (await db.execute(select(User).where(User.email == email))).scalars().all()
        )
    assert count == 1, "a duplicate registration must not create a second account"


@pytest.mark.asyncio
async def test_registration_rejects_a_weak_password(client):
    response = await client.post(
        "/api/auth/register", json={"email": fresh_email(), "password": "short"}
    )
    assert response.status_code == 400
    assert "10 characters" in response.json()["detail"]


@pytest.mark.asyncio
async def test_registration_rejects_a_malformed_address(client):
    response = await client.post(
        "/api/auth/register", json={"email": "not-an-address", "password": GOOD_PASSWORD}
    )
    assert response.status_code == 400


# ─── Verification ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_full_signup_then_verify_then_login(client, registered_emails):
    """The whole point of the feature, end to end."""
    email = fresh_email()
    registered_emails.append(email)

    registration = (
        await client.post(
            "/api/auth/register", json={"email": email, "password": GOOD_PASSWORD}
        )
    ).json()

    # Unverified accounts cannot sign in.
    blocked = await client.post(
        "/api/auth/login", json={"username": email, "password": GOOD_PASSWORD}
    )
    assert blocked.status_code == 403
    assert blocked.json()["detail"] == "EMAIL_NOT_VERIFIED"

    token = registration["verification_link"].split("token=", 1)[1]
    verified = await client.post("/api/auth/verify", json={"token": token})
    assert verified.status_code == 200
    assert verified.json()["verified"] is True

    # Now the address works as the login identifier.
    ok = await client.post(
        "/api/auth/login", json={"username": email, "password": GOOD_PASSWORD}
    )
    assert ok.status_code == 200
    body = ok.json()
    assert body["email"] == email
    assert body["role"] == "planner"
    assert "workorder:create" in body["permissions"]
    assert body["token"]


@pytest.mark.asyncio
async def test_login_is_case_insensitive_for_addresses(client, registered_emails):
    email = fresh_email()
    registered_emails.append(email)
    registration = (
        await client.post(
            "/api/auth/register", json={"email": email, "password": GOOD_PASSWORD}
        )
    ).json()
    token = registration["verification_link"].split("token=", 1)[1]
    await client.post("/api/auth/verify", json={"token": token})

    response = await client.post(
        "/api/auth/login",
        json={"username": email.upper(), "password": GOOD_PASSWORD},
    )
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_verifying_twice_reports_success(client, registered_emails):
    """Idempotent: from the user's point of view the address is confirmed either
    way, and an error here reads as "my account is broken"."""
    email = fresh_email()
    registered_emails.append(email)
    registration = (
        await client.post(
            "/api/auth/register", json={"email": email, "password": GOOD_PASSWORD}
        )
    ).json()
    token = registration["verification_link"].split("token=", 1)[1]

    assert (await client.post("/api/auth/verify", json={"token": token})).status_code == 200
    again = await client.post("/api/auth/verify", json={"token": token})
    assert again.status_code == 200
    assert again.json()["verified"] is True


@pytest.mark.asyncio
async def test_a_tampered_token_is_rejected(client):
    token = create_verification_token("attacker@example.test")
    payload, signature = token.split(".", 1)
    forged = f"{payload}.{signature[:-4]}XXXX"
    response = await client.post("/api/auth/verify", json={"token": forged})
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_verification_token_for_an_unknown_account_is_rejected(client):
    """A correctly signed token for an address with no account must not pass."""
    token = create_verification_token("never-registered@example.test")
    response = await client.post("/api/auth/verify", json={"token": token})
    assert response.status_code == 400


# ─── Token kind separation ────────────────────────────────────────────────────

def test_a_session_token_cannot_verify_an_account():
    assert decode_verification_token(create_token("someone@example.test", "planner")) is None


def test_a_verification_token_resolves_only_as_a_verification_token():
    token = create_verification_token("operator@example.test")
    assert decode_verification_token(token) == "operator@example.test"


@pytest.mark.asyncio
async def test_a_verification_token_is_not_accepted_as_a_session_credential():
    """Verification links are emailed, so they are far more exposed than a session
    token. Both are signed with the same secret, so the payload's `kind` is what
    keeps them apart."""
    class _Request:
        headers = {"Authorization": f"Bearer {create_verification_token('x@example.test')}"}

    assert await get_current_user(_Request()) is None


@pytest.mark.asyncio
async def test_a_session_token_still_authenticates():
    class _Request:
        headers = {"Authorization": f"Bearer {create_token('planner', 'planner')}"}

    user = await get_current_user(_Request())
    assert user is not None
    assert user["sub"] == "planner"
    assert user["role"] == "planner"


# ─── Seeded operator accounts are unaffected ──────────────────────────────────

@pytest.mark.asyncio
async def test_seeded_operators_have_no_email_and_still_sign_in(client):
    """They predate registration, so the verification gate must not lock them out."""
    async with AsyncSessionLocal() as db:
        planner = (
            await db.execute(select(User).where(User.username == "planner"))
        ).scalar_one()
    assert planner.email is None

    response = await client.post(
        "/api/auth/login", json={"username": "planner", "password": "predictops"}
    )
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_registration_can_be_disabled(client, monkeypatch):
    """A public URL may not want open signup."""
    import app.routes.auth as auth_routes

    monkeypatch.setattr(
        auth_routes, "settings", Settings(registration_enabled=False), raising=False
    )
    response = await client.post(
        "/api/auth/register", json={"email": fresh_email(), "password": GOOD_PASSWORD}
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "REGISTRATION_DISABLED"
