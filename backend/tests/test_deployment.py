"""
Deployment-path tests.

These cover code that only executes when the application runs somewhere other than
a developer's machine: a hosted Postgres URL, a reverse proxy in front of the
process, and demo accounts whose password is set from the environment. None of it
is exercised by the local SQLite defaults, so without these tests the first
evidence of a mistake would be a broken deployment.
"""
import pytest
import pytest_asyncio
from starlette.datastructures import Headers

from app.config import Settings
from app.database import (
    init_db,
    normalise_database_url,
    sync_database_url,
    uses_transaction_pooler,
)


# ─── Database URL normalisation ───────────────────────────────────────────────

@pytest.mark.parametrize(
    "raw, expected_scheme",
    [
        # The scheme Render and Heroku actually emit. SQLAlchemy 2.0 removed the
        # `postgres` alias, so this is the failure most deployments hit first.
        ("postgres://u:p@host/db", "postgresql+asyncpg://"),
        # A bare postgresql:// selects psycopg2, which cannot be driven by
        # create_async_engine.
        ("postgresql://u:p@host/db", "postgresql+asyncpg://"),
        ("postgresql+asyncpg://u:p@host/db", "postgresql+asyncpg://"),
    ],
)
def test_postgres_urls_resolve_to_the_async_driver(raw, expected_scheme):
    url, _ = normalise_database_url(raw)
    assert url.startswith(expected_scheme)


def test_sslmode_is_translated_for_asyncpg():
    """`?sslmode=require` is a libpq parameter. asyncpg rejects it outright, so it
    has to move into connect_args — but as the libpq *mode*, not a boolean.

    `ssl=True` in asyncpg means encrypt AND verify the certificate, whereas libpq's
    `require` means encrypt only. Collapsing require -> True was stricter than the
    URL asked for and failed against a live Supabase pooler with
    `CERTIFICATE_VERIFY_FAILED: self-signed certificate in certificate chain`.
    """
    url, connect_args = normalise_database_url(
        "postgres://u:p@dpg-x.oregon-postgres.render.com/db?sslmode=require"
    )
    assert "sslmode" not in url
    assert connect_args == {"ssl": "require"}


@pytest.mark.parametrize(
    "mode, expected",
    [
        ("require", "require"),
        ("verify-ca", "verify-ca"),
        ("verify-full", "verify-full"),
        ("prefer", "prefer"),
    ],
)
def test_stricter_ssl_modes_are_preserved(mode, expected):
    """Verification must not be silently downgraded either."""
    _, connect_args = normalise_database_url(f"postgres://u:p@host/db?sslmode={mode}")
    assert connect_args["ssl"] == expected


def test_sslmode_disable_does_not_request_tls():
    _, connect_args = normalise_database_url("postgres://u:p@host/db?sslmode=disable")
    assert "ssl" not in connect_args


# ─── Supabase ─────────────────────────────────────────────────────────────────

SUPABASE_SESSION = (
    "postgresql://postgres.abcdefgh:pw@aws-0-ap-northeast-2.pooler.supabase.com"
    ":5432/postgres"
)
SUPABASE_TRANSACTION = (
    "postgresql://postgres.abcdefgh:pw@aws-0-ap-northeast-2.pooler.supabase.com"
    ":6543/postgres"
)


def test_supabase_gets_tls_even_without_sslmode():
    """Supabase mandates TLS but the strings it offers for copying omit sslmode.
    `require` rather than verification, because the pooler presents a chain Python's
    default trust store rejects as self-signed."""
    _, connect_args = normalise_database_url(SUPABASE_SESSION)
    assert connect_args["ssl"] == "require"


def test_session_pooler_is_not_treated_as_a_transaction_pooler():
    """Port 5432 on the pooler host is session mode: one server connection per
    client session, so prepared statements behave normally."""
    url, connect_args = normalise_database_url(SUPABASE_SESSION)
    assert uses_transaction_pooler(url) is False
    assert "prepared_statement_cache_size" not in connect_args


def test_transaction_pooler_disables_prepared_statements():
    """Port 6543 hands a server connection to a different client between statements.
    asyncpg names prepared statements in numeric order, so without this the names
    collide across clients and queries fail with DuplicatePreparedStatementError."""
    url, connect_args = normalise_database_url(SUPABASE_TRANSACTION)
    assert uses_transaction_pooler(url) is True
    assert connect_args["prepared_statement_cache_size"] == 0
    assert connect_args["statement_cache_size"] == 0
    assert callable(connect_args["prepared_statement_name_func"])


def test_transaction_pooler_statement_names_are_unique():
    """Colliding names are the failure being prevented, so the generator must not
    return the same name twice."""
    _, connect_args = normalise_database_url(SUPABASE_TRANSACTION)
    make_name = connect_args["prepared_statement_name_func"]
    names = {make_name() for _ in range(200)}
    assert len(names) == 200


def test_supabase_sync_url_keeps_sslmode_for_psycopg2():
    """The seeder and trainer go through psycopg2, which needs libpq's spelling."""
    sync = sync_database_url(SUPABASE_SESSION)
    assert sync.startswith("postgresql+psycopg2://")
    assert "sslmode=require" in sync


def test_libpq_only_parameters_are_dropped():
    url, _ = normalise_database_url(
        "postgres://u:p@host/db?channel_binding=require&target_session_attrs=rw"
    )
    assert "channel_binding" not in url
    assert "target_session_attrs" not in url


def test_other_query_parameters_are_preserved():
    url, _ = normalise_database_url("postgres://u:p@host/db?application_name=predictops")
    assert "application_name=predictops" in url


def test_sqlite_urls_are_unchanged_apart_from_the_async_driver():
    assert normalise_database_url("sqlite:///./x.db")[0] == "sqlite+aiosqlite:///./x.db"
    assert normalise_database_url("sqlite+aiosqlite:///./x.db")[0] == (
        "sqlite+aiosqlite:///./x.db"
    )


def test_sync_url_uses_a_synchronous_driver():
    """The generators and the trainer are synchronous scripts. The previous derivation
    was a bare string replacement that only handled SQLite."""
    assert sync_database_url("sqlite+aiosqlite:///./x.db") == "sqlite:///./x.db"
    assert sync_database_url("postgres://u:p@host/db").startswith(
        "postgresql+psycopg2://"
    )


def test_sync_url_restores_sslmode_for_psycopg2():
    """psycopg2 wants the libpq spelling that asyncpg rejected."""
    assert "sslmode=require" in sync_database_url(
        "postgres://u:p@host/db?sslmode=require"
    )


# ─── Caller identity behind a reverse proxy ──────────────────────────────────

class _FakeRequest:
    """Minimal stand-in: client_key only reads the peer address and one header."""

    def __init__(self, peer: str, forwarded: str = None):
        self.client = type("C", (), {"host": peer})()
        self.headers = Headers({"x-forwarded-for": forwarded} if forwarded else {})


def _client_key_with_hops(request, hops: int, monkeypatch) -> str:
    import app.security as security

    settings = Settings(trusted_proxy_hops=hops)
    monkeypatch.setattr(security, "get_settings", lambda: settings)
    return security.client_key(request)


def test_zero_hops_ignores_the_forwarded_header(monkeypatch):
    """The default must not trust a header any client can set."""
    request = _FakeRequest("10.0.0.1", forwarded="1.2.3.4")
    assert _client_key_with_hops(request, 0, monkeypatch) == "10.0.0.1"


def test_one_hop_uses_the_client_address_the_proxy_recorded(monkeypatch):
    """Render terminates TLS at one proxy. Without this every visitor shares a
    single rate-limit bucket, so a few users lock out everyone else."""
    request = _FakeRequest("10.0.0.1", forwarded="203.0.113.7")
    assert _client_key_with_hops(request, 1, monkeypatch) == "203.0.113.7"


def test_a_spoofed_entry_cannot_displace_the_proxy_recorded_address(monkeypatch):
    """A client that forges the header only prepends to it: the trusted proxy
    appends the address it actually saw, so reading from the right still yields the
    real caller and the forged value is ignored."""
    request = _FakeRequest("10.0.0.1", forwarded="1.2.3.4, 203.0.113.7")
    assert _client_key_with_hops(request, 1, monkeypatch) == "203.0.113.7"


def test_a_header_shorter_than_the_trusted_chain_falls_back_to_the_peer(monkeypatch):
    """Fewer entries than configured hops means the request did not arrive through
    the expected chain, so no entry in it can be trusted."""
    request = _FakeRequest("10.0.0.1", forwarded="203.0.113.7")
    assert _client_key_with_hops(request, 2, monkeypatch) == "10.0.0.1"


def test_missing_header_falls_back_to_the_peer(monkeypatch):
    assert _client_key_with_hops(_FakeRequest("10.0.0.1"), 1, monkeypatch) == "10.0.0.1"


# ─── CORS allowlist normalisation ─────────────────────────────────────────────

def test_trailing_slash_is_stripped_from_allowed_origins():
    """A browser's Origin header is scheme+host+port with no path, so an entry ending
    in `/` can never match. Observed on a live deployment: the dashboard showed the
    right site but every preflight returned 400 "Disallowed CORS origin"."""
    s = Settings(cors_allowed_origins="https://predict-ops-six.vercel.app/")
    assert s.allowed_origins == ["https://predict-ops-six.vercel.app"]


def test_multiple_origins_are_each_normalised():
    s = Settings(
        cors_allowed_origins=
        " https://a.vercel.app/ , https://b.vercel.app ,, http://localhost:5173/ "
    )
    assert s.allowed_origins == [
        "https://a.vercel.app",
        "https://b.vercel.app",
        "http://localhost:5173",
    ]


def test_wildcard_is_still_downgraded_outside_development():
    """Normalising must not weaken the existing wildcard guard."""
    s = Settings(cors_allowed_origins="*", environment="production")
    assert s.allowed_origins == ["http://localhost:5173", "http://127.0.0.1:5173"]


def test_empty_allowlist_falls_back_to_the_dev_origin():
    assert Settings(cors_allowed_origins="").allowed_origins == ["http://localhost:5173"]


# ─── Demo account password alignment ─────────────────────────────────────────

@pytest_asyncio.fixture
async def restored_demo_users():
    """Snapshot the demo accounts' credentials and put them back afterwards.

    These tests deliberately rewrite stored password hashes, and the suite runs
    against the development database. Without this the local demo login would be
    left on whatever password the last test happened to set.
    """
    from sqlalchemy import select

    from app.database import AsyncSessionLocal
    from app.models.auth import User
    from app.services.user_seed import DEMO_USERS

    await init_db()
    names = [u[0] for u in DEMO_USERS]

    async with AsyncSessionLocal() as db:
        rows = (await db.execute(select(User).where(User.username.in_(names)))).scalars()
        snapshot = {r.username: (r.password_salt, r.password_hash) for r in rows}

    yield

    async with AsyncSessionLocal() as db:
        rows = (await db.execute(select(User).where(User.username.in_(names)))).scalars().all()
        for row in rows:
            if row.username in snapshot:
                row.password_salt, row.password_hash = snapshot[row.username]
        await db.commit()


@pytest.mark.asyncio
async def test_demo_accounts_track_the_configured_password(restored_demo_users, monkeypatch):
    """A deployment that changes DEMO_PASSWORD must not leave the accounts on the
    old one.

    This previously failed in both directions: the new password did not work, and —
    the security half — an account first seeded with the shipped default kept
    authenticating with that published default afterwards. The startup guard
    inspects the setting, but the stored hash is what authenticates.
    """
    from sqlalchemy import select

    import app.services.user_seed as user_seed
    from app.database import AsyncSessionLocal
    from app.models.auth import User
    from app.services.auth_service import verify_password

    async def reseed_with(password: str) -> None:
        monkeypatch.setattr(
            user_seed, "settings", Settings(demo_password=password), raising=False
        )
        await user_seed.seed_demo_users()

    async def stored_user() -> User:
        async with AsyncSessionLocal() as db:
            return (
                await db.execute(select(User).where(User.username == "planner"))
            ).scalar_one()

    await reseed_with("first-password")
    user = await stored_user()
    assert verify_password("first-password", user.password_salt, user.password_hash)

    await reseed_with("rotated-password")
    user = await stored_user()
    assert verify_password("rotated-password", user.password_salt, user.password_hash)
    assert not verify_password(
        "first-password", user.password_salt, user.password_hash
    ), "the superseded password must stop working"


@pytest.mark.asyncio
async def test_reseeding_an_unchanged_password_reports_no_writes(
    restored_demo_users, monkeypatch
):
    """Realignment must be a no-op on a steady-state boot, not a write every time."""
    import app.services.user_seed as user_seed

    monkeypatch.setattr(
        user_seed, "settings", Settings(demo_password="steady-password"), raising=False
    )
    await user_seed.seed_demo_users()
    assert await user_seed.seed_demo_users() == 0
