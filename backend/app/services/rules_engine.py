"""
Rules Engine — converts ML probability into risk score, severity, and alert creation decisions.
Business logic is completely separate from the ML model.
"""
from typing import Optional

# ─── Risk Score ───────────────────────────────────────────────────────────────

def probability_to_risk_score(probability: float) -> float:
    """Convert model probability (0-1) to risk score (0-100)."""
    return round(probability * 100, 1)


# ─── Risk Classification ──────────────────────────────────────────────────────

# Lower bound of each band per spec §25. Declared as data so the transparency
# endpoint can publish the exact thresholds `classify_risk` applies, rather than
# a second copy that could drift out of step.
CRITICAL_THRESHOLD = 75
WARNING_THRESHOLD = 45
WATCH_THRESHOLD = 25

RISK_BANDS = (
    {"severity": "Critical", "min": CRITICAL_THRESHOLD, "max": 100},
    {"severity": "Warning", "min": WARNING_THRESHOLD, "max": CRITICAL_THRESHOLD - 1},
    {"severity": "Watch", "min": WATCH_THRESHOLD, "max": WARNING_THRESHOLD - 1},
    {"severity": "Normal", "min": 0, "max": WATCH_THRESHOLD - 1},
)


def classify_risk(risk_score: float) -> str:
    """
    Risk bands per spec §25:
    0-24   → Normal
    25-44  → Watch
    45-74  → Warning
    75-100 → Critical
    """
    if risk_score >= CRITICAL_THRESHOLD:
        return "Critical"
    elif risk_score >= WARNING_THRESHOLD:
        return "Warning"
    elif risk_score >= WATCH_THRESHOLD:
        return "Watch"
    else:
        return "Normal"


# ─── Alert Creation Decision ──────────────────────────────────────────────────

def should_create_alert(
    risk_score: float,
    watch_streak: int = 0,
) -> bool:
    """
    Spec §26 rules:
    - Critical (>=75): always create alert
    - Warning (>=45): always create alert
    - Watch (25-44): only if watch condition persists for 3+ consecutive readings
    - Normal (<25): no alert
    """
    severity = classify_risk(risk_score)
    if severity == "Critical":
        return True
    elif severity == "Warning":
        return True
    elif severity == "Watch":
        return watch_streak >= 3
    else:
        return False


# ─── Failure Mode Inference ───────────────────────────────────────────────────

FAILURE_MODES = [
    "BEARING_DEGRADATION",
    "OVERHEATING",
    "MOTOR_DEGRADATION",
    "MISALIGNMENT",
    "GENERAL_MECHANICAL_WEAR",
]


def infer_failure_mode(
    vibration_pct: float,
    temperature_pct: float,
    rpm_cv: float,
    days_since_maintenance: float,
    machine_type: str,
) -> str:
    """
    Rule-based failure mode inference from the sensor deviation profile (spec §16).

    Inputs are scale-free so the rules work across machine types:
      vibration_pct   — vibration deviation from nominal, in percent
      temperature_pct — temperature deviation from nominal, in percent
      rpm_cv          — RPM standard deviation as a fraction of nominal RPM

    Absolute per-reading slopes were previously used, but on hourly data the
    per-step change is far below any meaningful threshold, so every machine
    collapsed to GENERAL_MECHANICAL_WEAR. Deviation shape is what actually
    distinguishes the modes: which sensors move, and in what proportion.
    """
    strong_vibration = vibration_pct > 60

    # Vibration climbs while temperature and speed stay steady: vibration is
    # the only sensor that really moves.
    if strong_vibration and temperature_pct < 12 and rpm_cv < 0.018:
        return "MISALIGNMENT"

    # Temperature is the dominant signal.
    if temperature_pct >= 25 and temperature_pct > vibration_pct * 0.5:
        return "OVERHEATING"

    # Speed instability dominates, with vibration secondary.
    if rpm_cv >= 0.018 and vibration_pct > 20:
        return "MOTOR_DEGRADATION"

    # Vibration and temperature rising together.
    if strong_vibration and temperature_pct >= 8:
        return "BEARING_DEGRADATION"

    return "GENERAL_MECHANICAL_WEAR"
