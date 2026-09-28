"""
Model-free deviation scoring.

The trained model learned from synthetic data with authored degradation patterns,
so its accuracy on an unseen factory is unproven. It also cannot extrapolate: even
with the inference guard, a value beyond training is only ever treated as "as bad
as the worst case in evidence", and the model is not monotonic in vibration inside
its own range.

This module provides a second, independent signal that has none of those
properties. It is plain arithmetic over the machine's own reference values:

    deviation = weighted, bounded distance of current sensors from this machine's
                own normal

It requires no training, is monotonic by construction, cannot be out of
distribution, and can be checked by hand from the numbers shown on screen. Where
the ML score and the deviation score agree, confidence is high. Where they
disagree, that disagreement is itself worth surfacing rather than hiding.
"""
import logging
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Relative influence of each sensor on the combined score. Vibration leads because
# mechanical wear shows there first; RPM instability is weighted lowest because it
# is often a control-system artefact rather than a fault.
SENSOR_WEIGHTS = {
    "vibration": 0.45,
    "temperature": 0.35,
    "rpm": 0.20,
}

# Fractional deviation treated as "fully deviated" (score 100 for that sensor).
# Vibration commonly doubles before failure, so 100% over reference saturates;
# temperature rising 50% over normal is already severe.
FULL_SCALE_DEVIATION = {
    "vibration": 1.00,
    "temperature": 0.50,
    "rpm": 0.30,
}

# Below this the reading is treated as normal, absorbing ordinary sensor noise so
# a healthy machine does not accumulate a nonzero score.
NOISE_FLOOR = 0.05


def _sensor_component(current: Optional[float], reference: Optional[float], sensor: str) -> Dict[str, float]:
    """Bounded 0-100 deviation for one sensor, plus the raw fraction."""
    if current is None or reference is None or reference == 0:
        return {"sensor": sensor, "fraction": 0.0, "score": 0.0, "available": False}

    # RPM matters in both directions: too slow and too fast are both faults.
    fraction = (current - reference) / abs(reference)
    magnitude = abs(fraction) if sensor == "rpm" else max(fraction, 0.0)

    if magnitude <= NOISE_FLOOR:
        score = 0.0
    else:
        span = FULL_SCALE_DEVIATION[sensor] - NOISE_FLOOR
        score = min((magnitude - NOISE_FLOOR) / span, 1.0) * 100.0

    return {
        "sensor": sensor,
        "fraction": round(fraction, 4),
        "score": round(score, 1),
        "available": True,
    }


def deviation_score(
    current: Dict[str, Optional[float]],
    reference: Dict[str, Optional[float]],
) -> Dict[str, object]:
    """Combined 0-100 deviation of a reading from a machine's own normal.

    `current` and `reference` both carry vibration, temperature and rpm. Returns
    the score, per-sensor components and the dominant sensor, so a report can show
    the arithmetic rather than assert a conclusion.
    """
    components = [
        _sensor_component(current.get(sensor), reference.get(sensor), sensor)
        for sensor in SENSOR_WEIGHTS
    ]

    usable = [c for c in components if c["available"]]
    if not usable:
        return {
            "score": 0.0,
            "components": components,
            "dominant_sensor": None,
            "method": "unavailable_no_reference",
        }

    # Renormalise across available sensors so a missing channel does not silently
    # dilute the score toward zero.
    total_weight = sum(SENSOR_WEIGHTS[c["sensor"]] for c in usable)
    score = sum(SENSOR_WEIGHTS[c["sensor"]] * c["score"] for c in usable) / total_weight

    dominant = max(usable, key=lambda c: SENSOR_WEIGHTS[c["sensor"]] * c["score"])

    return {
        "score": round(score, 1),
        "components": components,
        "dominant_sensor": dominant["sensor"] if dominant["score"] > 0 else None,
        "method": "weighted_reference_deviation",
    }


# ─── Confidence in the ML score ───────────────────────────────────────────────

# The 24-reading rolling window feature engineering needs. Below this, rolling
# means, standard deviations and slopes are computed on a partial window and are
# weaker than anything the model saw in training.
MIN_HISTORY_FOR_FULL_FEATURES = 24

# Machine types present in training. Anything else gets an all-zero one-hot, so
# the model has no type signal for it.
KNOWN_MACHINE_TYPES = {
    "CNC Machine",
    "Hydraulic Press",
    "Conveyor Motor",
    "Industrial Pump",
}


def assess_confidence(
    reading_count: int,
    machine_type: Optional[str],
    reference_source: str,
    maintenance_available: bool,
    range_exceedances: Optional[List[dict]] = None,
    missing_sensors: Optional[List[str]] = None,
) -> Dict[str, object]:
    """How much the ML score can be relied on, and why.

    Deliberately computed from evidence rather than from data origin. A judge who
    uploads six months of CNC history gets higher confidence than a demo machine
    with no maintenance records. The rule does not know or care where the data
    came from.
    """
    reasons: List[str] = []
    score = 100

    # A missing sensor channel is not a partial handicap: the model was fitted on
    # all three together and cannot be evaluated without them, so there is no ML
    # score to have confidence in.
    if missing_sensors:
        return {
            "confidence": "not_applicable",
            "confidence_score": 0,
            "reasons": [
                f"no {', '.join(missing_sensors)} channel on this machine, so the ML "
                "model cannot be evaluated; the deviation score and absolute limits "
                "are used instead"
            ],
            "scoring_basis": "model_unavailable_use_deviation_and_absolute_limits",
            "ml_available": False,
        }

    if reading_count < MIN_HISTORY_FOR_FULL_FEATURES:
        score -= 40
        reasons.append(
            f"only {reading_count} readings; rolling features need "
            f"{MIN_HISTORY_FOR_FULL_FEATURES} for a full window"
        )
    elif reading_count < 100:
        score -= 15
        reasons.append(f"limited history ({reading_count} readings)")

    if machine_type not in KNOWN_MACHINE_TYPES:
        score -= 25
        reasons.append(
            f"machine type '{machine_type or 'Unknown'}' was not in training, so the "
            "model has no type signal for it"
        )

    if reference_source == "assumed_default":
        score -= 20
        reasons.append("deviations measured against assumed defaults, not a supplied spec or derived baseline")
    elif reference_source == "derived_baseline":
        score -= 5
        reasons.append("reference derived from the machine's own history rather than a design spec")

    if not maintenance_available:
        score -= 10
        reasons.append("no maintenance history, so that feature was neutralised")

    if range_exceedances:
        score -= 15
        features = ", ".join(sorted({e["feature"] for e in range_exceedances})[:3])
        reasons.append(f"readings exceeded the model's training range and were clamped ({features})")

    score = max(0, min(score, 100))
    level = "high" if score >= 75 else "medium" if score >= 45 else "low"

    return {
        "confidence": level,
        "confidence_score": score,
        "reasons": reasons,
        "ml_available": True,
        # What the ML number is actually grounded in. The model was trained on
        # synthetic data, so it is never "validated" on an unseen factory.
        "scoring_basis": (
            "model_trained_on_synthetic_data"
            if level != "low"
            else "model_transfer_weak_prefer_deviation_score"
        ),
    }


# ─── Absolute limits ──────────────────────────────────────────────────────────
#
# Relative deviation has a blind spot: if every reading supplied was already
# degraded, the derived baseline encodes the fault as "normal" and the machine
# looks healthy against itself. A machine running at 115 C for its whole recorded
# history scores zero deviation, which is exactly wrong.
#
# Absolute limits close that gap. These are published engineering thresholds, not
# invented numbers, so they can be cited and argued with.

# ISO 10816-1 vibration velocity zone boundaries (mm/s RMS), Class II machines
# (medium machines, 15-75 kW). Zone C is "unsatisfactory", zone D "unacceptable".
ISO_10816_ZONE_C = 2.8
ISO_10816_ZONE_D = 7.1

# Most mineral-oil lubricants degrade rapidly beyond about 100 C, and bearing
# surface temperatures above roughly 90 C are conventionally treated as a concern.
TEMPERATURE_CONCERN_C = 90.0
TEMPERATURE_SEVERE_C = 100.0


def absolute_concerns(current: Dict[str, Optional[float]]) -> List[Dict[str, object]]:
    """Concerns raised by absolute sensor values, independent of any baseline.

    Deliberately baseline-free. This is the check that still works when the
    supplied history contains no healthy period to learn from.
    """
    concerns: List[Dict[str, object]] = []

    vibration = current.get("vibration")
    if vibration is not None:
        if vibration >= ISO_10816_ZONE_D:
            concerns.append({
                "sensor": "vibration",
                "value": round(float(vibration), 2),
                "severity": "severe",
                "threshold": ISO_10816_ZONE_D,
                "basis": "ISO 10816-1 zone D (unacceptable) for Class II machines",
            })
        elif vibration >= ISO_10816_ZONE_C:
            concerns.append({
                "sensor": "vibration",
                "value": round(float(vibration), 2),
                "severity": "concern",
                "threshold": ISO_10816_ZONE_C,
                "basis": "ISO 10816-1 zone C (unsatisfactory) for Class II machines",
            })

    temperature = current.get("temperature")
    if temperature is not None:
        if temperature >= TEMPERATURE_SEVERE_C:
            concerns.append({
                "sensor": "temperature",
                "value": round(float(temperature), 1),
                "severity": "severe",
                "threshold": TEMPERATURE_SEVERE_C,
                "basis": "above ~100 C most mineral-oil lubricants degrade rapidly",
            })
        elif temperature >= TEMPERATURE_CONCERN_C:
            concerns.append({
                "sensor": "temperature",
                "value": round(float(temperature), 1),
                "severity": "concern",
                "threshold": TEMPERATURE_CONCERN_C,
                "basis": "bearing surface temperature above ~90 C is conventionally a concern",
            })

    return concerns


def absolute_score(concerns: List[Dict[str, object]]) -> float:
    """Collapse absolute concerns into a 0-100 figure comparable with the others."""
    if not concerns:
        return 0.0
    return 100.0 if any(c["severity"] == "severe" for c in concerns) else 60.0


def detect_signal_disagreement(ml_risk: float, deviation: float) -> Optional[Dict[str, object]]:
    """Flag cases where the two independent signals contradict each other.

    This exists because the fitted model is measurably non-monotonic: a Conveyor
    Motor at 95 C scores ~0.1% while the same machine at 90 C scores ~98%, and
    95 C sits INSIDE the training range, so no amount of input clamping repairs it.

    The dangerous direction is a low model score with a high deviation: sensors are
    clearly abnormal but the model calls the machine fine. Trusting the model
    silently there would mean reporting an overheating machine as safe.
    """
    ml = float(ml_risk or 0.0)
    dev = float(deviation or 0.0)

    if dev >= 60.0 and ml < 25.0:
        return {
            "kind": "model_understates",
            "ml_risk": round(ml, 1),
            "deviation_score": round(dev, 1),
            "message": (
                f"Sensors deviate strongly from this machine's normal "
                f"(deviation {dev:.0f}/100) but the model scores it {ml:.0f}%. "
                "Treat the deviation score as authoritative here: the model is "
                "known to be non-monotonic at extreme sensor values."
            ),
            "recommended_signal": "deviation_score",
        }

    if ml >= 75.0 and dev <= 20.0:
        return {
            "kind": "model_overstates",
            "ml_risk": round(ml, 1),
            "deviation_score": round(dev, 1),
            "message": (
                f"The model scores this {ml:.0f}% but sensors sit close to this "
                f"machine's normal (deviation {dev:.0f}/100). The score is likely "
                "driven by maintenance or context features rather than current "
                "sensor condition."
            ),
            "recommended_signal": "investigate_both",
        }

    return None


ASSUMPTIONS = {
    "sensor_weights": SENSOR_WEIGHTS,
    "full_scale_deviation": FULL_SCALE_DEVIATION,
    "noise_floor": NOISE_FLOOR,
    "basis": (
        "The deviation score is plain arithmetic against each machine's own "
        "reference values. It requires no training, is monotonic by construction, "
        "and can be recomputed by hand from the readings shown. It is independent "
        "of the ML model and stays meaningful on data the model never saw."
    ),
}
