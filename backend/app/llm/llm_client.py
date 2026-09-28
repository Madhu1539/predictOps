"""
LLM provider layer.

The Investigation tab and alert explanations need a language model to phrase
evidence. They previously called Gemini directly, whose free tier allows **20
requests per day** — measured, `GenerateRequestsPerDayPerProjectPerModel-FreeTier`,
limit 20. That cap was reached during development, so every answer fell back to the
deterministic template.

Snowflake Cortex has no equivalent per-day free cap and reuses the connection the
project already has, so it is preferred. Gemini is kept as a second option and the
deterministic answer remains the final fallback, so the feature degrades in phrasing
rather than in substance.

Provider order is `cortex -> gemini -> none`, overridable via `LLM_PROVIDER`.

Reaching Cortex needs a connection the server can open without a human. Where the
only connections use browser OAuth, run `python -m app.llm.prime_cortex_login` once:
it completes the browser login and caches a reusable token, after which the guard on
interactive authenticators stands down and startup can open Cortex silently.
"""
import asyncio
import json
import logging
import os
import threading
from pathlib import Path
from typing import List, Optional, Tuple

from app.config import get_settings

logger = logging.getLogger(__name__)

CONNECTIONS_TOML = Path.home() / ".snowflake" / "connections.toml"

# Authenticators that require a human to complete a browser flow. A long-running
# server cannot do that: measured, attempting three such connections with expired
# cached tokens blocked for 3415 seconds waiting for callbacks that never arrived.
# They are skipped unless explicitly opted into, OR unless a cached token exists
# (see `_token_cache_has_entries`), because then the login is silent.
INTERACTIVE_AUTHENTICATORS = (
    "externalbrowser",
    "oauth_authorization_code",
    "username_password_mfa",
)

# Where the reusable OAuth/MFA token is kept.
#
# The connector will not choose this on its own. `TokenCache.make()` hardcodes
# `KeyringTokenCache` on Windows and macOS and only ever builds a `FileTokenCache`
# on Linux — but the keyring backend cannot hold a Snowflake OAuth token on Windows:
# `CredWrite` caps a credential blob at 2560 bytes and keyring encodes it as UTF-16,
# so the ~1.6 KB token exceeds the cap once encoded and the write raises
# `(1783, 'CredWrite', 'The stub received bad data')`. That exception propagates out
# of `connect()`, so installing keyring made Cortex *less* reachable, not more: the
# login itself succeeded and only the cache write failed.
#
# `FileTokenCache` is a first-class class in the same module and works on every
# platform; it is simply not selected here. Forcing it is measured to persist a
# token across separate processes on Windows (first open 10.0s with a browser,
# second open 3.3s with none).
TOKEN_CACHE_DIR = Path.home() / ".snowflake" / "predictops_token_cache"
TOKEN_CACHE_FILE = "credential_cache_v1.json"

_token_cache_installed = False

# Hard ceiling on connection attempts, so a stalled handshake can never hang
# startup even when an interactive connection is opted into.
LOGIN_TIMEOUT_SECONDS = 15
NETWORK_TIMEOUT_SECONDS = 20

# Cortex is reached over a normal Snowflake connection, which is synchronous and
# whose first handshake measured ~18s against ~2.5s once warm. The connection is
# therefore created once and reused, and every call runs in a worker thread so the
# event loop is never blocked.
_connection = None
_connection_name: Optional[str] = None
_connection_lock = threading.Lock()
_cortex_disabled = False


def _install_token_cache() -> bool:
    """Force the connector onto a file-backed token cache. Idempotent.

    Returns whether a cache is in place. Failure is not fatal: without a cache the
    connector falls back to `NoopTokenCache`, which means a browser prompt per
    process rather than a broken connection.
    """
    global _token_cache_installed
    if _token_cache_installed:
        return True

    try:
        import snowflake.connector.token_cache as token_cache
    except Exception as exc:  # pragma: no cover - defensive
        logger.info(f"Token cache unavailable: {str(exc)[:140]}")
        return False

    try:
        TOKEN_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        # The connector reads this env var when locating a cache directory.
        os.environ["SF_TEMPORARY_CREDENTIAL_CACHE_DIR"] = str(TOKEN_CACHE_DIR)

        cache = token_cache.FileTokenCache.make(
            # The check asserts POSIX modes. Windows has none to report, so the
            # directory always looks like 777 and the check always fails, emitting
            # an alarming warning before the fallback succeeds anyway. Skip it there
            # and keep it where it means something.
            skip_file_permissions_check=(os.name == "nt")
        )
        if cache is None:
            cache = token_cache.FileTokenCache.make(skip_file_permissions_check=True)
        if cache is None:
            logger.info("Could not build a file token cache; logins will not be cached.")
            return False

        token_cache.TokenCache.make = staticmethod(
            lambda skip_file_permissions_check=False: cache
        )
    except Exception as exc:
        logger.info(f"Could not install file token cache: {str(exc)[:140]}")
        return False

    _token_cache_installed = True
    logger.info(f"Snowflake token cache at {TOKEN_CACHE_DIR}")
    return True


def _token_cache_has_entries() -> bool:
    """Whether any token is cached, so an interactive login would likely be silent.

    A heuristic, deliberately: the cache keys are opaque hashes, so a cached token
    cannot be attributed to a particular connection name. The cost of being wrong is
    bounded — a connection that still needs a browser fails on `LOGIN_TIMEOUT_SECONDS`
    and the next provider is tried.
    """
    path = TOKEN_CACHE_DIR / TOKEN_CACHE_FILE
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, OSError):
        return False
    return bool(payload.get("tokens"))


def _parse_connections() -> List[Tuple[str, str]]:
    """`(name, authenticator)` for each entry in connections.toml, in file order."""
    entries: List[Tuple[str, str]] = []
    current: Optional[str] = None
    authenticator = ""
    try:
        for line in CONNECTIONS_TOML.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                if current is not None:
                    entries.append((current, authenticator))
                current = stripped[1:-1].strip('"')
                authenticator = ""
            elif current is not None and stripped.lower().startswith("authenticator"):
                _, _, value = stripped.partition("=")
                authenticator = value.strip().strip('"').strip("'").lower()
        if current is not None:
            entries.append((current, authenticator))
    except FileNotFoundError:
        logger.info("No connections.toml found; Cortex unavailable.")
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(f"Could not read connections.toml: {exc}")
    return entries


_candidates_cache: Optional[List[str]] = None


def _candidate_connection_names() -> List[str]:
    """Connection names to try, most specific first.

    No name is hardcoded: the file may hold several accounts and the intended one
    changes when the user switches. An explicit setting wins, then the standard
    environment variable, then whatever the file defines in order.

    Connections whose authenticator needs a browser are excluded, because a server
    cannot complete that handshake and attempting it blocks rather than failing.
    An explicit setting overrides the exclusion, since that is a deliberate choice.

    Cached: this is consulted on every availability check, and re-logging the
    exclusions each time buried everything else in the log.
    """
    global _candidates_cache
    if _candidates_cache is not None:
        return _candidates_cache

    settings = get_settings()
    explicit = getattr(settings, "snowflake_connection", "") or os.environ.get(
        "SNOWFLAKE_DEFAULT_CONNECTION_NAME", ""
    )
    if explicit:
        _candidates_cache = [explicit]
        return _candidates_cache

    # A cached token turns a browser login into a silent one, which is the whole
    # point of the cache. Without one, interactive connections are still excluded.
    cached_login_available = _token_cache_has_entries()

    usable: List[str] = []
    skipped: List[str] = []
    for name, authenticator in _parse_connections():
        if authenticator in INTERACTIVE_AUTHENTICATORS and not cached_login_available:
            skipped.append(f"{name} ({authenticator})")
            continue
        usable.append(name)

    if skipped:
        logger.info(
            "Cortex skipping %d connection(s) needing an interactive browser login: %s. "
            "A server cannot complete that handshake. Run "
            "`python -m app.llm.prime_cortex_login` once to cache a reusable token, "
            "or use key-pair auth or a programmatic access token, or set "
            "SNOWFLAKE_CONNECTION to opt in anyway.",
            len(skipped), ", ".join(skipped),
        )
    _candidates_cache = usable
    return _candidates_cache


def _get_connection():
    """Open (or reuse) a Snowflake connection, trying each candidate in turn."""
    global _connection, _connection_name, _cortex_disabled

    if _cortex_disabled:
        return None
    if _connection is not None:
        return _connection

    with _connection_lock:
        if _connection is not None:
            return _connection
        try:
            import snowflake.connector
        except ImportError:
            logger.info("snowflake-connector-python not installed; Cortex unavailable.")
            _cortex_disabled = True
            return None

        _install_token_cache()

        for name in _candidate_connection_names():
            try:
                candidate = snowflake.connector.connect(
                    connection_name=name,
                    # Never let a stalled handshake hang the process.
                    login_timeout=LOGIN_TIMEOUT_SECONDS,
                    network_timeout=NETWORK_TIMEOUT_SECONDS,
                    # Reuse the cached token instead of prompting per process.
                    client_store_temporary_credential=True,
                )
            except Exception as exc:
                logger.info(f"Snowflake connection '{name}' would not open: {str(exc)[:140]}")
                continue

            # Probe Cortex itself, not `SELECT 1`. Every account passes `SELECT 1`,
            # including ones where Cortex is not enabled — measured, an account
            # returned "Unknown user-defined function SNOWFLAKE.CORTEX.COMPLETE"
            # while connecting perfectly. Validating on connectivity alone cached a
            # connection that could never answer.
            try:
                cursor = candidate.cursor()
                cursor.execute(
                    "SELECT SNOWFLAKE.CORTEX.COMPLETE(%s, %s)",
                    (get_settings().cortex_model, "ok"),
                )
                cursor.fetchone()
                cursor.close()
            except Exception as exc:
                logger.info(
                    f"Snowflake connection '{name}' cannot run Cortex: {str(exc)[:140]}"
                )
                try:
                    candidate.close()
                except Exception:
                    pass
                continue

            _connection = candidate
            _connection_name = name
            logger.info(f"Cortex LLM using Snowflake connection '{name}'.")
            return _connection

        logger.info(
            "No Snowflake connection can run Cortex; falling back to Gemini, then "
            "to deterministic answers."
        )
        _cortex_disabled = True
        return None


def reset_connection() -> None:
    """Drop the cached connection, so a switched account is picked up (and tests).

    The token cache is left installed: it is process-wide connector state, not
    per-connection, and re-installing it on every reset would churn the env var.
    """
    global _connection, _connection_name, _cortex_disabled, _candidates_cache
    with _connection_lock:
        if _connection is not None:
            try:
                _connection.close()
            except Exception:
                pass
        _connection = None
        _connection_name = None
        _cortex_disabled = False
        _candidates_cache = None


def _cortex_complete_sync(prompt: str) -> str:
    """Blocking Cortex call. Always invoked from a worker thread."""
    settings = get_settings()
    connection = _get_connection()
    if connection is None:
        raise RuntimeError("CORTEX_UNAVAILABLE")

    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT SNOWFLAKE.CORTEX.COMPLETE(%s, %s)",
            (settings.cortex_model, prompt),
        )
        row = cursor.fetchone()
        return (row[0] or "").strip() if row else ""
    finally:
        cursor.close()


async def call_cortex_async(prompt: str) -> str:
    """Cortex COMPLETE, off the event loop."""
    return await asyncio.to_thread(_cortex_complete_sync, prompt)


def cortex_available() -> bool:
    """Whether Cortex is a usable provider.

    Deliberately does not open a connection: called on request paths, and an 18s
    handshake there would be worse than skipping the provider.
    """
    settings = get_settings()
    if settings.llm_provider not in ("auto", "cortex"):
        return False
    if _cortex_disabled:
        return False
    if _connection is not None:
        return True
    return bool(_candidate_connection_names())


def gemini_configured() -> bool:
    settings = get_settings()
    if settings.llm_provider not in ("auto", "gemini"):
        return False
    return bool(settings.gemini_api_key)


def llm_available() -> bool:
    """True when any provider might answer. The deterministic path covers the rest."""
    if get_settings().llm_provider == "none":
        return False
    return cortex_available() or gemini_configured()


async def call_llm_async(prompt: str) -> Tuple[str, str]:
    """Phrase a prompt with the first provider that succeeds.

    Returns `(text, provider)` so the UI can show which engine answered, rather
    than implying one and using another. Raises only when every provider fails, and
    callers treat that as "use the deterministic answer".
    """
    settings = get_settings()
    errors: List[str] = []

    order: List[str] = []
    if settings.llm_provider == "auto":
        order = ["cortex", "gemini"]
    elif settings.llm_provider in ("cortex", "gemini"):
        order = [settings.llm_provider]

    for provider in order:
        try:
            if provider == "cortex":
                if not cortex_available():
                    continue
                text = await call_cortex_async(prompt)
            else:
                if not gemini_configured():
                    continue
                from app.llm.explain_service import call_gemini_async
                text = await call_gemini_async(prompt)
            if text:
                return text, provider
            errors.append(f"{provider}: empty response")
        except Exception as exc:
            # Quota exhaustion and transient errors look the same to the caller:
            # try the next provider, then fall through to deterministic.
            errors.append(f"{provider}: {str(exc)[:160]}")
            logger.warning(f"LLM provider {provider} failed: {str(exc)[:200]}")

    raise RuntimeError("ALL_LLM_PROVIDERS_FAILED: " + "; ".join(errors) if errors
                       else "NO_LLM_PROVIDER_CONFIGURED")


async def warm_up() -> None:
    """Open the Cortex connection at startup so the first question is not slow.

    The handshake measured ~18s cold versus ~2.5s warm, which would otherwise land
    on whichever user asked first.
    """
    if not cortex_available():
        return
    try:
        await asyncio.to_thread(_get_connection)
        if _connection is not None:
            logger.info("Cortex connection warmed.")
    except Exception as exc:
        logger.info(f"Cortex warm-up skipped: {str(exc)[:160]}")
