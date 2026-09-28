"""
Inference module — loads model artifact and produces risk score.
Uses the same feature_engineering module as training to prevent mismatch.
"""
import joblib
import logging
import numpy as np
import pandas as pd
from pathlib import Path
from typing import Optional, Dict

from app.ml.feature_engineering import (
    engineer_features,
    prepare_feature_matrix,
    compute_maintenance_context,
    FEATURE_COLUMNS,
)

logger = logging.getLogger(__name__)

_model = None
_model_path: Optional[Path] = None


def load_model(model_path: str = None) -> object:
    """Load (or reload) the model artifact."""
    global _model, _model_path

    if model_path is None:
        from app.config import get_settings
        model_path = get_settings().model_path

    path = Path(model_path)
    if not path.is_absolute():
        # Resolve relative to backend/ directory
        path = Path(__file__).parent.parent.parent / model_path

    if not path.exists():
        raise FileNotFoundError(f"Model artifact not found at {path}. Run train.py first.")

    _model = joblib.load(path)
    _model_path = path
    logger.info(f"Model loaded from {path}")
    return _model


def get_model():
    """Get cached model, loading if needed."""
    global _model
    if _model is None:
        _model = load_model()
    return _model


def predict_risk(feature_row: pd.DataFrame) -> float:
    """
    Predict failure risk probability for a single feature row.
    Returns risk_score 0-100.
    """
    risk, _ = predict_risk_guarded(feature_row)
    return risk


def predict_risk_guarded(feature_row: pd.DataFrame) -> tuple:
    """Predict risk, clamping inputs to the model's training range.

    Returns `(risk_score, exceedances)`. Without the clamp, a value beyond the
    training range can land in an arbitrary leaf: an overheating machine at 95 C
    scored 0.1% while the same machine at 90 C scored 98%. Clamping makes the
    answer "at least as severe as the worst case in evidence" and reports that the
    input exceeded what the model can justify.
    """
    from app.ml.guardrails import clamp_features

    model = get_model()
    X = prepare_feature_matrix(feature_row)
    X_guarded, exceedances = clamp_features(X)
    prob = model.predict_proba(X_guarded)[0][1]  # probability of class 1 (failure)
    return round(float(prob) * 100, 1), exceedances


def _build_feature_frame(
    readings_df: pd.DataFrame,
    machine_meta: Dict,
    maintenance_df: pd.DataFrame,
) -> pd.DataFrame:
    """Assemble the engineered feature frame from raw readings.

    Shared by scoring and attribution so the two can never disagree about the
    inputs, in the same spirit as `compute_maintenance_context`.
    """
    df = readings_df.copy()

    # Add machine context columns
    df["machine_type"] = machine_meta.get("type", "CNC Machine")
    df["criticality"] = machine_meta.get("criticality", "Medium")
    df["nominal_vibration"] = machine_meta.get("nominal_vibration", 2.0)
    df["nominal_temperature"] = machine_meta.get("nominal_temperature", 65.0)
    df["nominal_rpm"] = machine_meta.get("nominal_rpm", 1450.0)

    install_date = pd.to_datetime(machine_meta.get("install_date", "2020-01-01"))
    df["install_date"] = install_date
    df["machine_age_days"] = (df["timestamp"] - install_date).dt.days

    # Maintenance features via the shared helper, so inference and training
    # cannot drift apart. No fallback is passed: absent records mean "unknown",
    # and the helper substitutes a neutral in-distribution value. Passing machine
    # age here previously placed the row in a region where the model had learned
    # a spurious "no records => safe" rule and returned 0% risk regardless of
    # sensor condition.
    maint_context = compute_maintenance_context(
        as_of=df["timestamp"].max(),
        maintenance_df=maintenance_df,
    )
    for key, value in maint_context.items():
        # `maintenance_context_available` is provenance, not a model feature.
        if key == "maintenance_context_available":
            continue
        df[key] = value

    # Apply shared feature engineering
    return engineer_features(df)


def predict_risk_from_readings(
    readings_df: pd.DataFrame,
    machine_meta: Dict,
    maintenance_df: pd.DataFrame,
) -> float:
    """
    Full inference from raw readings.
    Applies identical feature engineering as training.
    Returns risk_score 0-100.
    """
    df = _build_feature_frame(readings_df, machine_meta, maintenance_df)

    # Use the last row (most recent reading)
    last_row = df.tail(1)
    return predict_risk(last_row)


def predict_risk_with_attribution(
    readings_df: pd.DataFrame,
    machine_meta: Dict,
    maintenance_df: pd.DataFrame,
) -> tuple:
    """Same inference as `predict_risk_from_readings`, plus why.

    Returns `(risk_score, attribution_records, exceedances)`. The attribution is
    computed on the identical feature row that produced the score, so the
    explanation can never describe different inputs than the prediction used.
    `exceedances` lists any feature clamped to the training range.
    """
    df = _build_feature_frame(readings_df, machine_meta, maintenance_df)
    last_row = df.tail(1)

    from app.ml.attribution import attribute_prediction

    risk, exceedances = predict_risk_guarded(last_row)
    records = attribute_prediction(last_row)
    return risk, records, exceedances


def rules_based_risk_fallback(
    vibration: float,
    nominal_vibration: float,
    temperature: float,
    nominal_temperature: float,
    days_since_maintenance: float,
) -> float:
    """
    Fallback risk estimation when model is unavailable (spec §47).
    Simple heuristic — clearly labeled as degraded mode.
    """
    vib_ratio = vibration / max(nominal_vibration, 0.01)
    temp_ratio = temperature / max(nominal_temperature, 0.01)

    base_risk = 10.0
    if vib_ratio > 1.5:
        base_risk += 35.0
    elif vib_ratio > 1.25:
        base_risk += 20.0
    elif vib_ratio > 1.1:
        base_risk += 10.0

    if temp_ratio > 1.2:
        base_risk += 25.0
    elif temp_ratio > 1.1:
        base_risk += 12.0

    if days_since_maintenance > 90:
        base_risk += 20.0
    elif days_since_maintenance > 60:
        base_risk += 10.0

    return round(min(base_risk, 100.0), 1)
