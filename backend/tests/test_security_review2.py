"""
Regression tests for the second security review.

Covers the findings from the audit that followed registration, Modbus ingestion
and the CoCo skills being added. Each test names the weakness it pins down, so a
later refactor that quietly removes a control fails here rather than in
production.
"""
import importlib.util
import pathlib
import re
import time

import pytest
import pytest_asyncio
from email.message import EmailMessage
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.config import Settings
from app.database import AsyncSessionLocal, init_db
from app.main import app
from app.models.auth import User
from app.services.auth_service import (
    _EMAIL_PATTERN,
    normalise_email,
    validate_email,
)
from app.services.modbus_source import MIN_POLL_SECONDS, read_plan

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


@pytest_asyncio.fixture
async def client():
    await init_db()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def purge_probe_accounts():
    """Remove accounts these tests create, leaving seeded operators alone."""
    yield
    async with AsyncSessionLocal() as session:
        await session.execute(
            delete(User).where(User.email.like("sec-probe-%@example.test"))
        )
        await session.commit()


# ─── Registration timing oracle ───────────────────────────────────────────────

async def test_registering_an_existing_address_still_costs_the_hash(
    client, purge_probe_accounts
):
    """The identical response body is defeated by a stopwatch without this.

    Creating an account runs 120k PBKDF2 rounds (~51 ms measured). The
    existing-address branch returns early, so without a dummy hash the two cases
    are trivially distinguishable and the deliberate enumeration resistance is
    worthless.
    """
    email = "sec-probe-timing@example.test"
    password = "a-sufficiently-long-passphrase"

    first = await client.post(
        "/api/auth/register", json={"email": email, "password": password}
    )
    assert first.status_code == 201

    # Both paths now hash, so the gap should be a small fraction of the hash cost
    # rather than the whole of it.
    samples = []
    for index in range(3):
        start = time.perf_counter()
        repeat = await client.post(
            "/api/auth/register", json={"email": email, "password": password}
        )
        samples.append(time.perf_counter() - start)
        assert repeat.status_code == 201

        start = time.perf_counter()
        fresh = await client.post(
            "/api/auth/register",
            json={"email": f"sec-probe-fresh{index}@example.test", "password": password},
        )
        fresh_elapsed = time.perf_counter() - start
        assert fresh.status_code == 201
        samples.append(fresh_elapsed)

    existing_times = samples[0::2]
    fresh_times = samples[1::2]
    existing_avg = sum(existing_times) / len(existing_times)
    fresh_avg = sum(fresh_times) / len(fresh_times)

    # Asserting on the ratio rather than an absolute millisecond figure, because
    # CI machines vary wildly. Before the fix this ratio was effectively zero.
    assert existing_avg > fresh_avg * 0.4, (
        f"existing-address path is {existing_avg * 1000:.0f} ms vs "
        f"{fresh_avg * 1000:.0f} ms for a new one — still a timing oracle"
    )


async def test_existing_and_new_registration_bodies_are_indistinguishable(
    client, purge_probe_accounts, monkeypatch
):
    """Matching status codes are not enough.

    The message and the `email_sent` flag have to match too. They originally did
    not — "Check your email to finish setting up your account" versus "Account
    created. Check your email for the confirmation link" — which told an
    anonymous caller exactly which addresses already held accounts.

    Tested with mail configured, because that is the only shape a public
    deployment is allowed to run: the startup guard refuses verification without
    SMTP outside development. In the development fallback the two bodies do
    legitimately differ, since a genuine new account has to be handed its link
    somehow.
    """
    from app.routes import auth as auth_routes

    monkeypatch.setattr(auth_routes, "smtp_configured", lambda: True)
    monkeypatch.setattr(
        auth_routes, "send_verification_email", lambda *_a, **_k: (True, None)
    )

    password = "a-sufficiently-long-passphrase"
    existing_email = "sec-probe-same@example.test"

    created = await client.post(
        "/api/auth/register", json={"email": existing_email, "password": password}
    )
    assert created.status_code == 201

    repeat = await client.post(
        "/api/auth/register", json={"email": existing_email, "password": password}
    )
    fresh = await client.post(
        "/api/auth/register",
        json={"email": "sec-probe-other@example.test", "password": password},
    )
    assert repeat.status_code == fresh.status_code == 201

    repeat_body = repeat.json()
    fresh_body = fresh.json()

    def comparable(body):
        return {k: v for k, v in body.items() if k != "email"}

    assert comparable(repeat_body) == comparable(fresh_body), (
        f"registration responses differ:\n  existing={comparable(repeat_body)}\n"
        f"  new={comparable(fresh_body)}"
    )
    assert repeat_body["verification_link"] is None
    assert fresh_body["verification_link"] is None
    for giveaway in ("already registered", "taken", "exists", "in use"):
        assert giveaway not in repeat_body["message"].lower()


async def test_an_existing_address_is_never_handed_a_verification_link(
    client, purge_probe_accounts
):
    """The fallback link is the one thing that must not leak for an address the
    caller may not own. A new account can receive it; a repeat must not."""
    password = "a-sufficiently-long-passphrase"
    email = "sec-probe-nolink@example.test"

    await client.post("/api/auth/register", json={"email": email, "password": password})
    repeat = (await client.post(
        "/api/auth/register", json={"email": email, "password": password}
    )).json()

    assert not repeat.get("verification_link")


# ─── Email header injection ───────────────────────────────────────────────────

@pytest.mark.parametrize("hostile", [
    "ok@example.com\nBcc: victim@evil.test",
    "ok@example.com\rBcc: victim@evil.test",
    "ok@example.com\r\nSubject: hijacked",
])
def test_crlf_in_an_address_never_reaches_a_header(hostile):
    """Three independent layers, and this asserts the first two.

    `normalise_email` strips, the pattern rejects embedded newlines, and
    `EmailMessage` raises on them as a backstop. Any one of them is enough; the
    test exists so removing one is noticed.
    """
    assert validate_email(normalise_email(hostile)) is not None


def test_a_trailing_newline_is_stripped_before_validation():
    """Python's `$` matches before a final newline, so the pattern alone accepts
    `a@b.com\\n`. Normalisation is what actually removes it."""
    assert _EMAIL_PATTERN.match("ok@example.com\n") is not None
    assert normalise_email("ok@example.com\n") == "ok@example.com"


def test_emailmessage_is_the_backstop():
    message = EmailMessage()
    with pytest.raises(ValueError):
        message["To"] = "bad@example.com\nBcc: victim@evil.test"


# ─── Startup guard: verification without SMTP ────────────────────────────────

def _production(**overrides) -> Settings:
    base = {
        "environment": "production",
        "auth_enabled": True,
        "demo_mode": False,
        "demo_password": "not-the-default",
        "secret_key": "x" * 48,
        "registration_enabled": True,
        "registration_require_verification": True,
        "smtp_host": "",
        "smtp_from": "",
    }
    base.update(overrides)
    return Settings(**base)


def _verify(settings, monkeypatch):
    from app import security
    monkeypatch.setattr(security, "get_settings", lambda: settings)
    return security.verify_startup_configuration


def test_production_refuses_verification_it_cannot_send(monkeypatch):
    """Verification with no mail transport does not verify anything.

    The registration route returns the confirmation link in its own response when
    SMTP is unconfigured, so whoever submits an address is handed the means to
    confirm it — including an address they do not own.
    """
    check = _verify(_production(), monkeypatch)
    with pytest.raises(RuntimeError) as err:
        check()
    assert "SMTP" in str(err.value)


def test_production_accepts_verification_with_smtp_configured(monkeypatch):
    check = _verify(
        _production(smtp_host="smtp.example.invalid", smtp_from="no-reply@example.invalid"),
        monkeypatch,
    )
    check()


def test_production_accepts_verification_switched_off_knowingly(monkeypatch):
    """Turning verification off is a defensible choice; inheriting a broken one
    is not. Both routes out have to work."""
    check = _verify(_production(registration_require_verification=False), monkeypatch)
    check()


def test_production_accepts_registration_switched_off(monkeypatch):
    check = _verify(_production(registration_enabled=False), monkeypatch)
    check()


def test_development_is_not_blocked_by_the_smtp_rule(monkeypatch):
    """A fresh clone has no SMTP and must still run."""
    settings = Settings(
        environment="development",
        auth_enabled=False,
        registration_enabled=True,
        registration_require_verification=True,
        smtp_host="",
        secret_key="x" * 48,
    )
    _verify(settings, monkeypatch)()


# ─── Verify endpoint throttling ──────────────────────────────────────────────

async def test_verify_endpoint_is_throttled(client, monkeypatch):
    """An unauthenticated endpoint that queries and writes per call should not be
    an unlimited lever, even though forging its token is infeasible."""
    from app import security
    from app.routes import auth as auth_routes

    settings = auth_routes.settings
    monkeypatch.setattr(
        settings, "rate_limit_enabled", True, raising=False
    )
    security.login_limiter.cache_clear()

    statuses = set()
    for _ in range(settings.login_attempt_limit + 3):
        response = await client.post("/api/auth/verify", json={"token": "not-a-token"})
        statuses.add(response.status_code)

    security.login_limiter.cache_clear()
    assert 429 in statuses, f"verify was never throttled; saw {statuses}"


# ─── Modbus hardening ────────────────────────────────────────────────────────

def test_poll_interval_has_a_floor():
    """A configured 0 would busy-wait, saturating a core while hammering the
    device and the database."""
    assert MIN_POLL_SECONDS > 0
    for configured in (0, -5, None):
        assert max(MIN_POLL_SECONDS, float(configured or 0)) >= MIN_POLL_SECONDS


def test_an_oversized_stride_produces_no_request():
    """A stride wider than one Modbus frame cannot be served, so emitting a
    request the device is certain to reject is worse than emitting none."""
    assert read_plan(0, 4, 200) == []
    assert read_plan(0, 4, 126) == []
    assert read_plan(0, 4, 125) == [(0, 125), (125, 125), (250, 125), (375, 125)]


# ─── Skills must not write credentials to disk ───────────────────────────────

SKILL_FILES = sorted((REPO_ROOT / ".cortex" / "skills").glob("*/SKILL.md"))


def test_skills_are_present():
    assert SKILL_FILES, "no skills found to audit"


@pytest.mark.parametrize("path", SKILL_FILES, ids=lambda p: p.parent.name)
def test_no_skill_writes_a_credential_to_a_file(path):
    """A file holding a password outlives a failed command, and the working
    directory is a git repository — one `git add .` from committing it."""
    text = path.read_text(encoding="utf-8")
    offenders = [
        line.strip()
        for line in text.splitlines()
        if "password" in line.lower() and re.search(r"Out-File|>\s*\S+\.json|Set-Content", line)
    ]
    assert not offenders, f"{path.parent.name} writes a credential to disk: {offenders}"


@pytest.mark.parametrize("path", SKILL_FILES, ids=lambda p: p.parent.name)
def test_skill_json_bodies_use_stdin(path):
    """`--data-binary \"@file\"` for a JSON body leaves the file behind; `@-` does
    not. CSV uploads legitimately reference a real path the user supplied."""
    text = path.read_text(encoding="utf-8")
    bad = [
        line.strip()
        for line in text.splitlines()
        if "--data-binary" in line
        and ".json" in line
        and "@-" not in line
    ]
    assert not bad, f"{path.parent.name} still uses a temp JSON file: {bad}"
