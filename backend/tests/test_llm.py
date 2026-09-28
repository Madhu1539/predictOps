"""
Explanation service tests (spec §29-31).

The demo must never fail because Gemini is unavailable, so the deterministic
path and the cache are what these tests pin down.
"""
import pytest

from app.llm import explain_service
from app.llm.explain_service import _deterministic_explanation, get_explanation


BASE_ALERT = {
    "machine_name": "M-102",
    "risk_score": 87.0,
    "failure_mode": "BEARING_DEGRADATION",
    "days_since_maintenance": 63.0,
    "previous_failure_context": "Previous Bearing Degradation recorded",
    "recommended_action": "Inspect bearing assembly",
    "machine_criticality": "High",
    "production_impact": "High",
    "part_needed": "Bearing Assembly",
    "part_available": True,
    "sensor_deviations": '{"vibration_pct": 42.0, "temperature_pct": 18.0}',
}


def test_deterministic_explanation_is_grounded_in_alert_data():
    text = _deterministic_explanation(BASE_ALERT)
    assert "M-102" in text
    assert "87" in text
    assert "42" in text          # vibration deviation
    assert "63" in text          # days since maintenance
    assert "bearing" in text.lower()


def test_deterministic_explanation_survives_null_deviations():
    """An alert with no sensor deviations must not break the fallback."""
    alert = {**BASE_ALERT, "sensor_deviations": None}
    text = _deterministic_explanation(alert)
    assert len(text) > 40


def test_deterministic_explanation_survives_empty_alert():
    text = _deterministic_explanation({})
    assert isinstance(text, str) and len(text) > 0


def test_deterministic_explanation_survives_malformed_json():
    alert = {**BASE_ALERT, "sensor_deviations": "{not-valid-json"}
    text = _deterministic_explanation(alert)
    assert len(text) > 40


def test_deterministic_explanation_reports_unavailable_part():
    alert = {**BASE_ALERT, "part_available": False}
    text = _deterministic_explanation(alert).lower()
    assert "not in stock" in text or "unavailable" in text


@pytest.mark.asyncio
async def test_get_explanation_without_api_key_uses_fallback(monkeypatch):
    """With no Gemini key configured, the service must still return text."""
    monkeypatch.setattr(explain_service.settings, "gemini_api_key", "", raising=False)
    explain_service._cache.clear()

    text, source = await get_explanation(4242, BASE_ALERT)
    assert source == "deterministic"
    assert len(text) > 40


@pytest.mark.asyncio
async def test_get_explanation_caches_by_alert_id(monkeypatch):
    monkeypatch.setattr(explain_service.settings, "gemini_api_key", "", raising=False)
    explain_service._cache.clear()

    first, _ = await get_explanation(4243, BASE_ALERT)
    second, source = await get_explanation(4243, BASE_ALERT)
    assert second == first
    assert source == "cached"


@pytest.mark.asyncio
async def test_gemini_failure_falls_back_to_deterministic(monkeypatch):
    """A raising Gemini client must degrade, not propagate (spec §31)."""
    monkeypatch.setattr(explain_service.settings, "gemini_api_key", "fake-key", raising=False)
    explain_service._cache.clear()

    def _boom(*args, **kwargs):
        raise RuntimeError("gemini unavailable")

    monkeypatch.setattr(explain_service, "_call_gemini", _boom, raising=False)

    text, source = await get_explanation(4244, BASE_ALERT)
    assert source == "deterministic"
    assert len(text) > 40
