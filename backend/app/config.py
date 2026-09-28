from pydantic_settings import BaseSettings
from functools import lru_cache
from typing import List
import os


class Settings(BaseSettings):
    database_url: str = "sqlite+aiosqlite:///./predictops.db"
    gemini_api_key: str = ""
    # Stable alias rather than a pinned version: `gemini-1.5-flash` was retired
    # and every call began returning 404, which the deterministic fallback hid.
    # An alias keeps working as Google rotates model generations.
    gemini_model: str = "gemini-flash-latest"
    # Which language model phrases answers. "auto" prefers Snowflake Cortex and falls
    # back to Gemini, because the Gemini free tier allows only 20 requests per day
    # (measured) and that cap was reached in normal development use. Cortex has no
    # equivalent per-day free cap and reuses the existing Snowflake connection.
    # Values: auto | cortex | gemini | none
    llm_provider: str = "auto"
    cortex_model: str = "claude-sonnet-4-5"
    # Blank resolves from SNOWFLAKE_DEFAULT_CONNECTION_NAME, then connections.toml in
    # file order. Not hardcoded: the file commonly holds several accounts and the
    # intended one changes when the user switches.
    snowflake_connection: str = ""
    model_path: str = "app/ml/model.joblib"
    environment: str = "development"
    demo_mode: bool = True
    live_feed_interval_seconds: int = 15
    # Raise a work order automatically when a new Critical alert appears, rather
    # than waiting for a planner to click. This is the "automate work orders" half
    # of the problem statement: a Critical prediction that needs a human to become
    # actionable is a notification, not automation. Guarded against duplicates —
    # one open work order per machine — and set false to demo the manual flow.
    auto_work_order_enabled: bool = True
    # Severities that trigger automatic creation. Comma-separated. Warning is
    # excluded by default: at ~0.53 precision, auto-raising every Warning would
    # bury planners in work orders for alerts that resolve on their own.
    auto_work_order_severities: str = "Critical"
    # Auth gates MUTATING endpoints only; reads are always open so dashboards and
    # wall displays cannot break. Off by default because the bundled UI has no
    # login screen, and a demo where every acknowledgement returns 401 is worse
    # than one that trusts its operator. Set AUTH_ENABLED=true to enforce roles;
    # the audit trail is written either way.
    auth_enabled: bool = False
    # Never used as-is. `app.security.resolve_secret_key()` treats this literal as
    # "unset": in development it is replaced by a persisted random key, and outside
    # development startup refuses to run. A shipped default signing key means any
    # reader of this repository can mint a planner token.
    secret_key: str = "predictops-dev-secret-change-me"
    demo_password: str = "predictops"
    # Outbound notification for Critical alerts. Empty disables delivery, and the
    # attempt is recorded as "skipped" rather than silently dropped.
    notification_webhook_url: str = ""

    # ─── Hardening ────────────────────────────────────────────────────────────
    # Browser origins allowed to call the API. A wildcard cannot be combined with
    # credentials, and it lets any page a user visits drive this API against
    # localhost, so the dev server origins are named explicitly instead.
    # Comma-separated. "*" is honoured only in a development environment.
    cors_allowed_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    # Ceiling on a raw CSV request body. `request.body()` buffers in memory, so
    # without this one request can exhaust the process.
    max_upload_bytes: int = 8 * 1024 * 1024
    # Off in the test suite only; see tests/conftest.py.
    rate_limit_enabled: bool = True
    # Login attempts per client address per window, then a lockout. PBKDF2 at
    # 120k rounds also makes unthrottled login a CPU-exhaustion lever.
    login_attempt_limit: int = 10
    login_attempt_window_seconds: int = 300
    # LLM-backed endpoints spend money and third-party quota per call.
    llm_request_limit: int = 30
    llm_request_window_seconds: int = 60
    # Interface to bind when run via `python -m app.main`. Loopback by default:
    # binding every interface exposes an unauthenticated-by-default API to the
    # local network.
    host: str = "127.0.0.1"
    port: int = 8000
    # Number of reverse proxies in front of this process that append to
    # X-Forwarded-For. 0 means trust nothing and identify callers by peer address,
    # which is correct locally. Set to 1 on Render (and most single-proxy PaaS),
    # otherwise every visitor shares one rate-limit bucket. See
    # `app.security.client_key`.
    trusted_proxy_hops: int = 0

    model_config = {"env_file": ".env", "extra": "ignore", "protected_namespaces": ("settings_",)}

    @property
    def is_development(self) -> bool:
        return self.environment.strip().lower() in ("development", "dev", "local", "test")

    @property
    def auto_work_order_severity_set(self) -> set:
        """Severities that trigger automatic work-order creation."""
        return {
            s.strip()
            for s in (self.auto_work_order_severities or "").split(",")
            if s.strip()
        }

    @property
    def allowed_origins(self) -> List[str]:
        """Parsed CORS allowlist. A wildcard outside development is downgraded to
        the named defaults rather than silently trusted."""
        raw = [o.strip() for o in (self.cors_allowed_origins or "").split(",")]
        origins = [o for o in raw if o]
        if "*" in origins and not self.is_development:
            return ["http://localhost:5173", "http://127.0.0.1:5173"]
        return origins or ["http://localhost:5173"]



@lru_cache()
def get_settings() -> Settings:
    return Settings()
