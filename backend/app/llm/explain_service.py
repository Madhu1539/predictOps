"""
Explanation Service — generates natural-language alert explanations.
Primary: Gemini API
Fallback: Deterministic text (mandatory, must never fail).
Caches results by alert_id.
"""
import asyncio
import json
import logging
from typing import Dict, Optional
from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# In-memory cache (alert_id → explanation text)
_cache: Dict[int, str] = {}


def _deterministic_explanation(alert_data: dict) -> str:
    """
    Always-available fallback explanation built from structured alert data.
    No external dependencies.
    """
    machine = alert_data.get("machine_name", "This machine")
    risk = alert_data.get("risk_score") or 0
    failure_mode = alert_data.get("failure_mode", "GENERAL_MECHANICAL_WEAR")
    days_maint = alert_data.get("days_since_maintenance") or 0
    prev_context = alert_data.get("previous_failure_context", "")
    action = alert_data.get("recommended_action", "Perform inspection")
    part_available = alert_data.get("part_available", True)

    # Sensor deviations. The key may be present but null, so coalesce
    # explicitly rather than relying on a dict default.
    deviations = alert_data.get("sensor_deviations") or {}
    if isinstance(deviations, str):
        try:
            deviations = json.loads(deviations)
        except Exception:
            deviations = {}
    if not isinstance(deviations, dict):
        deviations = {}

    # A null percentage means the channel is not instrumented, which is different
    # from a measured zero, so those sensors are simply not described.
    vib_dev = deviations.get("vibration_pct")
    temp_dev = deviations.get("temperature_pct")
    rpm_dev = deviations.get("rpm_pct")
    rpm_cv = deviations.get("rpm_cv", 0) or 0

    # Build explanation
    parts = []
    if risk is None:
        parts.append(
            f"{machine} has no ML risk score: the model requires vibration, "
            "temperature and RPM together, and this machine does not report all "
            "three. The assessment below rests on measured sensor deviation and "
            "published limits instead."
        )
    else:
        parts.append(
            f"{machine} currently shows an estimated {risk:.0f}% failure risk over the next 7 days."
        )

    sensor_msgs = []
    if vib_dev is not None and vib_dev > 5:
        sensor_msgs.append(f"vibration is {vib_dev:.0f}% above its recent baseline")
    if temp_dev is not None and temp_dev > 5:
        sensor_msgs.append(f"temperature is trending {temp_dev:.0f}% above normal")
    if rpm_dev is not None and abs(rpm_dev) > 5:
        direction = "above" if rpm_dev > 0 else "below"
        sensor_msgs.append(f"RPM is running {abs(rpm_dev):.0f}% {direction} nominal speed")
    if rpm_cv > 0.02:
        sensor_msgs.append("speed is unstable")

    if sensor_msgs:
        parts.append("Sensor analysis shows " + " and ".join(sensor_msgs) + ".")

    if days_maint is None:
        # Absent records are not evidence of neglect; say what is actually known.
        parts.append(
            "No maintenance history is on record for this machine, so service "
            "interval could not be assessed."
        )
    elif days_maint > 30:
        parts.append(
            f"This machine has not received maintenance in {days_maint:.0f} days, making it overdue for service."
        )

    if prev_context:
        parts.append(f"Historical records note: {prev_context}.")

    if not part_available:
        parts.append("Note: the required spare part is currently not in stock — procurement should be initiated immediately.")

    # Recommendations already carry their own punctuation, so don't double it.
    parts.append(f"Recommended action: {str(action).rstrip('.')}.")

    return " ".join(parts)


def _format_top_factors(alert_data: dict, vib: float, temp: float, rpm: float) -> str:
    """Render the ranked factor list for the prompt.

    Uses model attribution when present; otherwise falls back to raw sensor
    deviations so the prompt is never empty.
    """
    records = alert_data.get("attribution")
    if isinstance(records, str):
        try:
            records = json.loads(records)
        except Exception:
            records = None

    if isinstance(records, list) and records:
        lines = []
        for record in records[:5]:
            if not isinstance(record, dict):
                continue
            label = record.get("label") or record.get("feature") or "factor"
            direction = "raises" if record.get("contribution", 0) > 0 else "lowers"
            lines.append(f"- {label}: {direction} risk (value {record.get('value')})")
        if lines:
            return "\n".join(lines)

    return (
        f"- Vibration: {vib:.0f}% vs baseline\n"
        f"- Temperature: {temp:.0f}% vs baseline\n"
        f"- RPM: {rpm:.0f}% vs baseline"
    )


def _build_gemini_prompt(alert_data: dict) -> str:
    """Build a structured prompt for Gemini per spec §30."""
    deviations = alert_data.get("sensor_deviations") or {}
    if isinstance(deviations, str):
        try:
            deviations = json.loads(deviations)
        except Exception:
            deviations = {}
    if not isinstance(deviations, dict):
        deviations = {}

    vib = deviations.get("vibration_pct", 0) or 0
    temp = deviations.get("temperature_pct", 0) or 0
    rpm = deviations.get("rpm_pct", 0) or 0

    # Prefer the model's own contributions over a fixed two-line list, so the
    # narrative reflects what actually drove the score.
    factors = _format_top_factors(alert_data, vib, temp, rpm)

    return f"""You are a predictive maintenance analyst. Write a concise 3-5 sentence explanation for a maintenance engineer based only on the following structured data. Do not invent facts.

Machine: {alert_data.get('machine_name', 'Unknown')}
Risk Score: {alert_data.get('risk_score', 0) or 0:.0f}%
Failure Mode: {alert_data.get('failure_mode', 'Unknown')}
Top Factors (ranked by the model's own contribution):
{factors}
Days since last maintenance: {alert_data.get('days_since_maintenance', 'Unknown')}
Previous failure context: {alert_data.get('previous_failure_context', 'None')}
Machine criticality: {alert_data.get('machine_criticality', 'Unknown')}
Production impact: {alert_data.get('production_impact', 'Unknown')}
Recommended action: {alert_data.get('recommended_action', 'Inspect machine')}
Part available: {alert_data.get('part_available', True)}
Estimated cost if it runs to failure: {alert_data.get('estimated_loss_avoided', 'Unknown')}

Write the explanation:"""


GEMINI_MODEL = settings.gemini_model


def _call_gemini(prompt: str) -> str:
    """Send a prompt to Gemini and return the trimmed text.

    Extracted so both the alert explanation and the investigation service share
    one implementation. Raises on any failure; callers are responsible for
    falling back to a deterministic path.
    """
    import google.generativeai as genai

    genai.configure(api_key=settings.gemini_api_key)
    model = genai.GenerativeModel(settings.gemini_model)
    response = model.generate_content(prompt)
    return response.text.strip()


async def call_gemini_async(prompt: str) -> str:
    """Off-thread wrapper.

    `generate_content` is a blocking network call; running it directly inside an
    async endpoint would stall the event loop and, with the live feed running,
    delay scoring for every machine.
    """
    return await asyncio.to_thread(_call_gemini, prompt)


def gemini_available() -> bool:
    return bool(settings.gemini_api_key)


async def get_explanation(alert_id: int, alert_data: dict) -> tuple[str, str]:
    """
    Returns (explanation_text, source) where source names the provider that
    answered — 'cortex', 'gemini', 'cached' or 'deterministic' — so the UI reports
    the engine actually used rather than the one preferred.
    Caches result by alert_id.
    """
    # Check cache first
    if alert_id in _cache:
        return _cache[alert_id], "cached"

    # Try the configured providers in order. Cortex is preferred because the Gemini
    # free tier permits only 20 requests per day, which is reached quickly in normal
    # use and silently forced every explanation onto the deterministic template.
    from app.llm.llm_client import call_llm_async, llm_available

    if llm_available():
        try:
            text, provider = await call_llm_async(_build_gemini_prompt(alert_data))
            if text:
                _cache[alert_id] = text
                return text, provider
        except Exception as e:
            logger.warning(
                f"LLM explanation failed for alert {alert_id}: {str(e)[:200]}. "
                "Using deterministic fallback."
            )

    # Deterministic fallback — always works
    text = _deterministic_explanation(alert_data)
    _cache[alert_id] = text
    return text, "deterministic"
