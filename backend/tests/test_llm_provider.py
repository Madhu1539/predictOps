"""
Tests for the LLM provider layer.

Every case here corresponds to a defect found while building it, because each one
fails silently or catastrophically rather than loudly:

- validating a connection with `SELECT 1` cached an account where Cortex does not
  exist ("Unknown user-defined function SNOWFLAKE.CORTEX.COMPLETE")
- attempting browser-auth connections from a server blocked for 3415 seconds waiting
  for callbacks that never arrived
- re-logging the exclusions on every availability check buried the rest of the log
- the keyring token cache cannot hold a Snowflake OAuth token on Windows (`CredWrite`
  caps a blob at 2560 bytes and keyring encodes it as UTF-16), and the write error
  propagated out of `connect()`
- reading the real token cache made three tests pass or fail according to whether the
  developer happened to be logged in
"""
import json

import pytest

from app.llm import llm_client


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch, tmp_path_factory):
    """Isolate connection state AND the token cache directory.

    The guard on interactive authenticators now consults the token cache, so without
    this the outcome depends on whether the developer has logged in — measured: three
    tests flipped from pass to fail purely because a real token had been cached.
    """
    monkeypatch.setattr(
        llm_client, "TOKEN_CACHE_DIR", tmp_path_factory.mktemp("token_cache")
    )
    llm_client.reset_connection()
    yield
    llm_client.reset_connection()


def _cache_token(monkeypatch, tmp_path, payload=None):
    """Put a cache file in place so an interactive login would be silent."""
    cache_dir = tmp_path / "primed_cache"
    cache_dir.mkdir(exist_ok=True)
    (cache_dir / llm_client.TOKEN_CACHE_FILE).write_text(
        json.dumps({"tokens": {"abc123": "a-token"}} if payload is None else payload),
        encoding="utf-8",
    )
    monkeypatch.setattr(llm_client, "TOKEN_CACHE_DIR", cache_dir)
    llm_client.reset_connection()
    return cache_dir


def _write_connections(tmp_path, monkeypatch, body: str):
    path = tmp_path / "connections.toml"
    path.write_text(body, encoding="utf-8")
    monkeypatch.setattr(llm_client, "CONNECTIONS_TOML", path)
    llm_client.reset_connection()
    return path


BROWSER_ONLY = """
[old-account]
account = "OLD"
authenticator = "oauth_authorization_code"

[other-account]
account = "OTHER"
authenticator = "externalbrowser"
"""

KEYPAIR = """
[browser-one]
account = "ONE"
authenticator = "oauth_authorization_code"

[service-account]
account = "TWO"
authenticator = "snowflake_jwt"
private_key_file = "/tmp/key.p8"
"""


def test_browser_authenticators_are_excluded(tmp_path, monkeypatch):
    """A server cannot complete an interactive login, and attempting it blocks
    rather than failing fast."""
    monkeypatch.delenv("SNOWFLAKE_DEFAULT_CONNECTION_NAME", raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "auto")
    monkeypatch.setenv("SNOWFLAKE_CONNECTION", "")
    _write_connections(tmp_path, monkeypatch, BROWSER_ONLY)

    assert llm_client._candidate_connection_names() == []


def test_cached_token_lets_browser_connections_through(tmp_path, monkeypatch):
    """The guard exists because an interactive login blocks. With a token cached it
    does not, so refusing the connection would give up Cortex for no reason."""
    monkeypatch.delenv("SNOWFLAKE_DEFAULT_CONNECTION_NAME", raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "auto")
    monkeypatch.setenv("SNOWFLAKE_CONNECTION", "")
    _cache_token(monkeypatch, tmp_path)
    _write_connections(tmp_path, monkeypatch, BROWSER_ONLY)

    assert llm_client._candidate_connection_names() == ["old-account", "other-account"]


def test_empty_token_map_does_not_count_as_cached(tmp_path, monkeypatch):
    """`{"tokens": {}}` is what the file looks like before any login, and treating it
    as primed would reinstate the blocking handshake the guard exists to prevent."""
    _cache_token(monkeypatch, tmp_path, payload={"tokens": {}})

    assert llm_client._token_cache_has_entries() is False


def test_malformed_token_cache_is_not_cached_and_not_fatal(tmp_path, monkeypatch):
    cache_dir = tmp_path / "broken"
    cache_dir.mkdir()
    (cache_dir / llm_client.TOKEN_CACHE_FILE).write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(llm_client, "TOKEN_CACHE_DIR", cache_dir)

    assert llm_client._token_cache_has_entries() is False


def test_absent_token_cache_is_not_cached(tmp_path, monkeypatch):
    monkeypatch.setattr(llm_client, "TOKEN_CACHE_DIR", tmp_path / "never-created")

    assert llm_client._token_cache_has_entries() is False


def test_install_token_cache_forces_the_file_backend(monkeypatch, tmp_path):
    """The connector hardcodes the keyring backend on Windows and macOS, and the
    keyring backend cannot hold a Snowflake OAuth token on Windows: `CredWrite` caps
    a credential blob at 2560 bytes, keyring encodes it as UTF-16, and the write
    raises \u2014 that error escapes `connect()`. So the file backend must actually be
    selected, not merely available.
    """
    token_cache = pytest.importorskip("snowflake.connector.token_cache")
    original_make = token_cache.TokenCache.make
    monkeypatch.setattr(llm_client, "TOKEN_CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(llm_client, "_token_cache_installed", False)
    try:
        assert llm_client._install_token_cache() is True
        assert isinstance(token_cache.TokenCache.make(), token_cache.FileTokenCache)
    finally:
        token_cache.TokenCache.make = original_make
        llm_client._token_cache_installed = False


def test_non_interactive_connection_is_selected(tmp_path, monkeypatch):
    monkeypatch.delenv("SNOWFLAKE_DEFAULT_CONNECTION_NAME", raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "auto")
    monkeypatch.setenv("SNOWFLAKE_CONNECTION", "")
    _write_connections(tmp_path, monkeypatch, KEYPAIR)

    assert llm_client._candidate_connection_names() == ["service-account"]


def test_explicit_setting_overrides_the_exclusion(tmp_path, monkeypatch):
    """Naming a connection is a deliberate choice, so the guard steps aside."""
    _write_connections(tmp_path, monkeypatch, BROWSER_ONLY)
    monkeypatch.setenv("SNOWFLAKE_DEFAULT_CONNECTION_NAME", "old-account")
    llm_client.reset_connection()

    assert llm_client._candidate_connection_names() == ["old-account"]


def test_authenticators_are_parsed_per_connection(tmp_path, monkeypatch):
    _write_connections(tmp_path, monkeypatch, KEYPAIR)
    parsed = dict(llm_client._parse_connections())
    assert parsed["browser-one"] == "oauth_authorization_code"
    assert parsed["service-account"] == "snowflake_jwt"


def test_exclusions_are_logged_once(tmp_path, monkeypatch, caplog):
    """The candidate list is consulted on every availability check; re-logging it
    each time drowned the log."""
    monkeypatch.delenv("SNOWFLAKE_DEFAULT_CONNECTION_NAME", raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "auto")
    monkeypatch.setenv("SNOWFLAKE_CONNECTION", "")
    _write_connections(tmp_path, monkeypatch, BROWSER_ONLY)

    with caplog.at_level("INFO", logger="app.llm.llm_client"):
        for _ in range(5):
            llm_client._candidate_connection_names()

    skips = [r for r in caplog.records if "skipping" in r.getMessage().lower()]
    assert len(skips) == 1, f"logged {len(skips)} times"


def test_skip_message_names_the_remedy(tmp_path, monkeypatch, caplog):
    """The skip is the exact point where the user can act, so the log has to say how.
    Without this the only symptom is that answers are quietly never phrased by Cortex.
    """
    monkeypatch.delenv("SNOWFLAKE_DEFAULT_CONNECTION_NAME", raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "auto")
    monkeypatch.setenv("SNOWFLAKE_CONNECTION", "")
    _write_connections(tmp_path, monkeypatch, BROWSER_ONLY)

    with caplog.at_level("INFO", logger="app.llm.llm_client"):
        llm_client._candidate_connection_names()

    assert any(
        "prime_cortex_login" in r.getMessage() for r in caplog.records
    ), "the skip message must name the command that fixes it"


def test_missing_connections_file_is_not_fatal(tmp_path, monkeypatch):
    monkeypatch.setattr(llm_client, "CONNECTIONS_TOML", tmp_path / "absent.toml")
    monkeypatch.delenv("SNOWFLAKE_DEFAULT_CONNECTION_NAME", raising=False)
    monkeypatch.setenv("SNOWFLAKE_CONNECTION", "")
    llm_client.reset_connection()

    assert llm_client._candidate_connection_names() == []
    assert llm_client.cortex_available() is False


def test_provider_none_disables_everything(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "none")
    from app.config import get_settings
    get_settings.cache_clear()

    assert llm_client.llm_available() is False


@pytest.mark.asyncio
async def test_all_providers_failing_raises_so_caller_falls_back(monkeypatch):
    """Callers treat the exception as 'use the deterministic answer', so it must be
    raised rather than swallowed into an empty string that looks like an answer."""
    monkeypatch.setenv("LLM_PROVIDER", "auto")
    from app.config import get_settings
    get_settings.cache_clear()

    monkeypatch.setattr(llm_client, "cortex_available", lambda: True)
    monkeypatch.setattr(llm_client, "gemini_configured", lambda: False)

    async def boom(_prompt):
        raise RuntimeError("cortex exploded")

    monkeypatch.setattr(llm_client, "call_cortex_async", boom)

    with pytest.raises(RuntimeError) as excinfo:
        await llm_client.call_llm_async("anything")
    assert "cortex exploded" in str(excinfo.value)


@pytest.mark.asyncio
async def test_provider_name_is_reported_so_the_badge_cannot_lie(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "auto")
    from app.config import get_settings
    get_settings.cache_clear()

    monkeypatch.setattr(llm_client, "cortex_available", lambda: True)

    async def ok(_prompt):
        return "phrased text"

    monkeypatch.setattr(llm_client, "call_cortex_async", ok)

    text, provider = await llm_client.call_llm_async("anything")
    assert text == "phrased text"
    assert provider == "cortex"


@pytest.mark.asyncio
async def test_gemini_is_used_when_cortex_is_unavailable(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "auto")
    from app.config import get_settings
    get_settings.cache_clear()

    monkeypatch.setattr(llm_client, "cortex_available", lambda: False)
    monkeypatch.setattr(llm_client, "gemini_configured", lambda: True)

    import app.llm.explain_service as explain

    async def fake_gemini(_prompt):
        return "gemini text"

    monkeypatch.setattr(explain, "call_gemini_async", fake_gemini)

    text, provider = await llm_client.call_llm_async("anything")
    assert text == "gemini text"
    assert provider == "gemini"


@pytest.mark.asyncio
async def test_warm_up_is_a_noop_when_cortex_is_unavailable(monkeypatch):
    """Warm-up runs at startup; it must never block when there is nothing to warm."""
    monkeypatch.setattr(llm_client, "cortex_available", lambda: False)
    await llm_client.warm_up()  # must return promptly and not raise
