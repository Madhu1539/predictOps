"""Tests for the one-time Cortex login priming command.

This command is the only place a browser prompt is acceptable, so what matters is that
it tries the connections the server refuses, and that it never claims success the
backend cannot reproduce.
"""
import pytest

from app.llm import llm_client, prime_cortex_login


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch, tmp_path_factory):
    monkeypatch.setattr(
        llm_client, "TOKEN_CACHE_DIR", tmp_path_factory.mktemp("token_cache")
    )
    llm_client.reset_connection()
    yield
    llm_client.reset_connection()


BROWSER_ONLY = """
[first-account]
account = "ONE"
authenticator = "oauth_authorization_code"

[second-account]
account = "TWO"
authenticator = "externalbrowser"
"""


def _write_connections(tmp_path, monkeypatch, body: str):
    path = tmp_path / "connections.toml"
    path.write_text(body, encoding="utf-8")
    monkeypatch.setattr(llm_client, "CONNECTIONS_TOML", path)
    llm_client.reset_connection()
    return path


def test_it_tries_the_connections_the_server_refuses(tmp_path, monkeypatch):
    """The whole point: these are exactly the connections `_candidate_connection_names`
    excludes, so priming them is impossible if this reuses the guarded list."""
    monkeypatch.delenv("SNOWFLAKE_DEFAULT_CONNECTION_NAME", raising=False)
    monkeypatch.setenv("SNOWFLAKE_CONNECTION", "")
    from app.config import get_settings
    get_settings.cache_clear()
    _write_connections(tmp_path, monkeypatch, BROWSER_ONLY)

    assert llm_client._candidate_connection_names() == []
    assert prime_cortex_login._names_to_try(["prog"]) == [
        "first-account",
        "second-account",
    ]


def test_an_explicit_argument_wins(tmp_path, monkeypatch):
    _write_connections(tmp_path, monkeypatch, BROWSER_ONLY)

    assert prime_cortex_login._names_to_try(["prog", "second-account"]) == [
        "second-account"
    ]


def test_blank_argument_is_ignored(tmp_path, monkeypatch):
    monkeypatch.delenv("SNOWFLAKE_DEFAULT_CONNECTION_NAME", raising=False)
    monkeypatch.setenv("SNOWFLAKE_CONNECTION", "")
    from app.config import get_settings
    get_settings.cache_clear()
    _write_connections(tmp_path, monkeypatch, BROWSER_ONLY)

    assert prime_cortex_login._names_to_try(["prog", "   "]) == [
        "first-account",
        "second-account",
    ]


def test_it_fails_loudly_when_nothing_was_cached(tmp_path, monkeypatch, capsys):
    """A login that works but caches nothing leaves the backend exactly as it was.
    Reporting success there would send the user away believing Cortex is wired up.
    """
    _write_connections(tmp_path, monkeypatch, BROWSER_ONLY)
    monkeypatch.setattr(llm_client, "_install_token_cache", lambda: True)
    monkeypatch.setattr(llm_client, "_token_cache_has_entries", lambda: False)

    class _Cursor:
        def execute(self, *_a, **_k):
            return None

        def fetchone(self):
            return ("ok",)

        def close(self):
            return None

    class _Connection:
        def cursor(self):
            return _Cursor()

        def close(self):
            return None

    import snowflake.connector

    monkeypatch.setattr(
        snowflake.connector, "connect", lambda **_kwargs: _Connection()
    )

    assert prime_cortex_login.main(["prog"]) == 1
    assert "no token was cached" in capsys.readouterr().err


def test_it_reports_success_when_a_token_lands(tmp_path, monkeypatch, capsys):
    _write_connections(tmp_path, monkeypatch, BROWSER_ONLY)
    monkeypatch.setattr(llm_client, "_install_token_cache", lambda: True)
    monkeypatch.setattr(llm_client, "_token_cache_has_entries", lambda: True)

    class _Cursor:
        def execute(self, *_a, **_k):
            return None

        def fetchone(self):
            return ("ok",)

        def close(self):
            return None

    class _Connection:
        def cursor(self):
            return _Cursor()

        def close(self):
            return None

    import snowflake.connector

    monkeypatch.setattr(
        snowflake.connector, "connect", lambda **_kwargs: _Connection()
    )

    assert prime_cortex_login.main(["prog"]) == 0
    assert "without prompting" in capsys.readouterr().out


def test_an_account_without_cortex_is_not_accepted(tmp_path, monkeypatch, capsys):
    """Measured: an account connected perfectly and returned "Unknown user-defined
    function SNOWFLAKE.CORTEX.COMPLETE". Priming a token for it helps nothing."""
    _write_connections(tmp_path, monkeypatch, BROWSER_ONLY)
    monkeypatch.setattr(llm_client, "_install_token_cache", lambda: True)
    monkeypatch.setattr(llm_client, "_token_cache_has_entries", lambda: True)

    class _Cursor:
        def execute(self, *_a, **_k):
            raise RuntimeError("Unknown user-defined function SNOWFLAKE.CORTEX.COMPLETE")

        def close(self):
            return None

    class _Connection:
        def cursor(self):
            return _Cursor()

        def close(self):
            return None

    import snowflake.connector

    monkeypatch.setattr(
        snowflake.connector, "connect", lambda **_kwargs: _Connection()
    )

    assert prime_cortex_login.main(["prog"]) == 1
    captured = capsys.readouterr()
    assert "cannot run Cortex" in captured.out
    # The feature must be described as still working, because it does.
    assert "deterministic" in captured.err


def test_no_connections_is_explained_not_crashed(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("SNOWFLAKE_DEFAULT_CONNECTION_NAME", raising=False)
    monkeypatch.setenv("SNOWFLAKE_CONNECTION", "")
    from app.config import get_settings
    get_settings.cache_clear()
    monkeypatch.setattr(llm_client, "CONNECTIONS_TOML", tmp_path / "absent.toml")
    monkeypatch.setattr(llm_client, "_install_token_cache", lambda: True)
    llm_client.reset_connection()

    assert prime_cortex_login.main(["prog"]) == 1
    assert "No connections found" in capsys.readouterr().err
