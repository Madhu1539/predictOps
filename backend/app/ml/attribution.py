"""
Per-prediction feature attribution (spec §29 explainability).

Answers "why is this machine at risk" from the model itself, rather than from a
parallel set of hand-written rules. Without this, `top_sensors` is a guess and
the explanation cannot honestly claim to reflect what the model used.

Deliberately dependency-free. The selected model is linear, so an exact
contribution is `coefficient x standardised feature value` — no SHAP required.
Tree models fall back to global `feature_importances_` weighted by the feature
value, which is an approximation and is labelled as such by `method`.
"""
import logging
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from app.ml.feature_engineering import FEATURE_COLUMNS, prepare_feature_matrix

logger = logging.getLogger(__name__)

# Feature name -> the sensor it belongs to, so contributions can be rolled up
# into the sensor-level `top_sensors` the UI and the alert use.
FEATURE_TO_SENSOR: Dict[str, str] = {
    "vibration": "vibration",
    "vibration_mean": "vibration",
    "vibration_std": "vibration",
    "vibration_slope": "vibration",
    "temperature": "temperature",
    "temperature_mean": "temperature",
    "temperature_std": "temperature",
    "temperature_slope": "temperature",
    "rpm": "rpm",
    "rpm_mean": "rpm",
    "rpm_std": "rpm",
    "rpm_slope": "rpm",
}

# Human-readable labels for the non-sensor features that often dominate.
FEATURE_LABELS: Dict[str, str] = {
    "days_since_last_maintenance": "Days since last maintenance",
    "maintenance_count": "Maintenance history depth",
    "previous_failure_count": "Previous failures",
    "machine_age_days": "Machine age",
    "criticality_encoded": "Machine criticality",
    "recent_downtime": "Recent downtime",
    "production_count": "Production volume",
    "machine_availability": "Availability",
    "vibration": "Vibration",
    "vibration_mean": "Vibration (24h mean)",
    "vibration_std": "Vibration variability",
    "vibration_slope": "Vibration trend",
    "temperature": "Temperature",
    "temperature_mean": "Temperature (24h mean)",
    "temperature_std": "Temperature variability",
    "temperature_slope": "Temperature trend",
    "rpm": "RPM",
    "rpm_mean": "RPM (24h mean)",
    "rpm_std": "RPM instability",
    "rpm_slope": "RPM trend",
}


def humanise(feature: str) -> str:
    if feature in FEATURE_LABELS:
        return FEATURE_LABELS[feature]
    if feature.startswith("machine_type_"):
        return f"Machine type: {feature.removeprefix('machine_type_')}"
    return feature.replace("_", " ").capitalize()


def _resolve_estimator(clf):
    """Unwrap a calibration wrapper to reach the underlying estimator.

    `CalibratedClassifierCV` holds the fitted base estimators in
    `calibrated_classifiers_`, so `coef_` is not on the wrapper itself.
    """
    if hasattr(clf, "coef_") or hasattr(clf, "feature_importances_"):
        return clf

    calibrated = getattr(clf, "calibrated_classifiers_", None)
    if calibrated:
        inner = calibrated[0]
        for attr in ("estimator", "base_estimator"):
            candidate = getattr(inner, attr, None)
            if candidate is not None:
                return candidate
    return clf


def attribute_prediction(
    feature_row: pd.DataFrame,
    model=None,
    top_n: int = 6,
) -> List[dict]:
    """Signed per-feature contributions for a single prediction.

    Returns a list of
    `{feature, label, value, contribution, direction, method}` sorted by
    absolute contribution, largest first. Returns an empty list rather than
    raising if the model cannot be introspected — attribution is additive
    context, and losing it must never break scoring.
    """
    try:
        if model is None:
            from app.ml.predict import get_model
            model = get_model()

        X = prepare_feature_matrix(feature_row)
        if X.empty:
            return []
        row = X.iloc[[0]]

        # `named_steps` is only present on a Pipeline; Random Forest has no
        # scaler step, so probe rather than assume.
        named = getattr(model, "named_steps", {})
        clf = named.get("clf", model)
        scaler = named.get("scaler")
        estimator = _resolve_estimator(clf)

        values = row.to_numpy(dtype=float)[0]

        if hasattr(estimator, "coef_"):
            basis = scaler.transform(row)[0] if scaler is not None else values
            contributions = np.asarray(estimator.coef_)[0] * basis
            method = "linear_coefficient"
        elif hasattr(estimator, "feature_importances_"):
            # Approximation: global importance scaled by the standardised value.
            basis = scaler.transform(row)[0] if scaler is not None else values
            contributions = np.asarray(estimator.feature_importances_) * basis
            method = "tree_importance_approx"
        else:
            logger.warning("Model exposes neither coef_ nor feature_importances_")
            return []

        records = []
        for name, value, contribution in zip(FEATURE_COLUMNS, values, contributions):
            contribution = float(contribution)
            records.append(
                {
                    "feature": name,
                    "label": humanise(name),
                    "value": round(float(value), 4),
                    "contribution": round(contribution, 4),
                    "direction": "increases_risk" if contribution > 0 else "reduces_risk",
                    "method": method,
                }
            )

        records.sort(key=lambda r: abs(r["contribution"]), reverse=True)
        return records[:top_n]

    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(f"Attribution unavailable: {exc}")
        return []


def top_sensors_from_attribution(records: List[dict]) -> List[str]:
    """Roll feature contributions up to sensor level, most influential first.

    Only counts contributions that push risk UP, since the question being
    answered is "which sensor is driving this alert".
    """
    totals: Dict[str, float] = {}
    for record in records:
        sensor = FEATURE_TO_SENSOR.get(record["feature"])
        if sensor is None:
            continue
        if record["contribution"] <= 0:
            continue
        totals[sensor] = totals.get(sensor, 0.0) + record["contribution"]

    return [s for s, _ in sorted(totals.items(), key=lambda kv: kv[1], reverse=True)]


def global_importance(model=None) -> Optional[List[dict]]:
    """Model-wide feature importance, for the transparency endpoint."""
    try:
        if model is None:
            from app.ml.predict import get_model
            model = get_model()

        named = getattr(model, "named_steps", {})
        estimator = _resolve_estimator(named.get("clf", model))

        if hasattr(estimator, "coef_"):
            weights = np.abs(np.asarray(estimator.coef_)[0])
            method = "abs_linear_coefficient"
        elif hasattr(estimator, "feature_importances_"):
            weights = np.asarray(estimator.feature_importances_)
            method = "tree_importance"
        else:
            return None

        records = [
            {"feature": name, "label": humanise(name), "weight": round(float(w), 5), "method": method}
            for name, w in zip(FEATURE_COLUMNS, weights)
        ]
        records.sort(key=lambda r: r["weight"], reverse=True)
        return records

    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(f"Global importance unavailable: {exc}")
        return None
