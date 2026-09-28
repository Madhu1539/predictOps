"""One-time interactive login that caches a reusable Snowflake token for Cortex.

    python -m app.llm.prime_cortex_login [connection-name]

Why this exists as a separate command rather than happening at startup: the server
must never open a browser and wait. `llm_client` therefore skips connections whose
authenticator needs a human, and only stops skipping them once a token is cached.
This command is the one place where a browser prompt is expected, so run it once and
the backend can use Cortex from then on.

Unlike the request path, this deliberately tries connections the guard excludes, and
allows a long login timeout, because a human is present.
"""
import logging
import sys
import time
from typing import List

from app.config import get_settings
from app.llm import llm_client

# A person is waiting at a browser, so allow far longer than the server ever would.
INTERACTIVE_LOGIN_TIMEOUT_SECONDS = 300


def _names_to_try(argv: List[str]) -> List[str]:
    if len(argv) > 1 and argv[1].strip():
        return [argv[1].strip()]
    explicit = get_settings().snowflake_connection
    if explicit:
        return [explicit]
    # Every connection, including the interactive ones the server refuses.
    return [name for name, _authenticator in llm_client._parse_connections()]


def main(argv: List[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if not llm_client._install_token_cache():
        print(
            "Could not create a token cache, so a login cannot be saved.\n"
            f"Expected to write to {llm_client.TOKEN_CACHE_DIR}.",
            file=sys.stderr,
        )
        return 1

    names = _names_to_try(argv)
    if not names:
        print(
            f"No connections found in {llm_client.CONNECTIONS_TOML}.\n"
            "Add one (`snow connection add`) and run this again.",
            file=sys.stderr,
        )
        return 1

    try:
        import snowflake.connector
    except ImportError:
        print("snowflake-connector-python is not installed.", file=sys.stderr)
        return 1

    model = get_settings().cortex_model
    print(f"Trying {len(names)} connection(s): {', '.join(names)}")
    print("A browser window may open. Complete the login there.\n")

    for name in names:
        started = time.time()
        try:
            connection = snowflake.connector.connect(
                connection_name=name,
                login_timeout=INTERACTIVE_LOGIN_TIMEOUT_SECONDS,
                network_timeout=INTERACTIVE_LOGIN_TIMEOUT_SECONDS,
                client_store_temporary_credential=True,
            )
        except Exception as exc:
            print(f"  {name}: would not open - {str(exc)[:160]}")
            continue

        # Probe Cortex itself. An account can connect perfectly and still not have
        # Cortex ("Unknown user-defined function SNOWFLAKE.CORTEX.COMPLETE"), and
        # caching a token for such an account would help nothing.
        try:
            cursor = connection.cursor()
            cursor.execute("SELECT SNOWFLAKE.CORTEX.COMPLETE(%s, %s)", (model, "ok"))
            cursor.fetchone()
            cursor.close()
        except Exception as exc:
            print(f"  {name}: connected but cannot run Cortex - {str(exc)[:160]}")
            try:
                connection.close()
            except Exception:
                pass
            continue

        elapsed = time.time() - started
        try:
            connection.close()
        except Exception:
            pass

        cached = llm_client._token_cache_has_entries()
        print(f"\n  {name}: Cortex reachable with '{model}' ({elapsed:.1f}s)")
        if cached:
            print(f"  Token cached in {llm_client.TOKEN_CACHE_DIR}")
            print("  The backend will now use Cortex without prompting.")
            return 0

        # The login worked but nothing was saved, so the next process prompts again.
        # Say so, rather than reporting success the backend cannot reproduce.
        print(
            "  WARNING: the login succeeded but no token was cached, so the backend\n"
            "  will still prompt. Cortex will fall back to Gemini, then to\n"
            "  deterministic answers.",
            file=sys.stderr,
        )
        return 1

    print(
        "\nNo connection could run Cortex. Gemini and the deterministic answers\n"
        "remain available, so the Investigation tab still works.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
