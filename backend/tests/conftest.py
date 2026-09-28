"""
Shared test configuration.

The suite must not call a language model. Tests that hit the Investigation tab or
alert explanations would otherwise make real Cortex and Gemini requests, which makes
them slow (measured: 70s to 127s), non-deterministic, and able to fail because a
quota was exhausted rather than because the code is wrong.

`LLM_PROVIDER=none` disables every provider, so `llm_available()` is False and each
feature returns its deterministic answer. Tests that specifically want to exercise
the LLM path stub the provider call and assert on the prompt, rather than opting
back into the network.
"""
import os

import pytest

# Applied at import time, before `app.config.get_settings` is first called and
# cached by lru_cache.
os.environ["LLM_PROVIDER"] = "none"
# The suite drives login and the LLM-backed endpoints far harder than a human
# would, so the throttles would fail tests for load rather than for defects. The
# limiter itself is covered directly in tests/test_security.py.
os.environ["RATE_LIMIT_ENABLED"] = "false"
# Keeps `resolve_secret_key` on its development branch, so the suite never needs a
# SECRET_KEY in the environment and never writes one.
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("SECRET_KEY", "test-only-signing-key-not-used-in-any-deployment")


@pytest.fixture(autouse=True)
def _no_network_llm(monkeypatch):
    """Belt and braces: keep providers off even if a test clears the setting."""
    monkeypatch.setenv("LLM_PROVIDER", "none")
