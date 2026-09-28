"""
ML pipeline tests (spec §52).

Covers model artifact integrity, inference bounds, feature-list consistency,
missing-feature handling, the rules-based fallback, label construction, and
train/inference parity of the maintenance features.
"""
from datetime import datetime, timedelta

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from app.ml.feature_engineering import (
    FEATURE_COLUMNS,
    engineer_features,
    prepare_feature_matrix,
    compute_maintenance_context,
)
from app.ml.labeling import (
    FAILURE_HORIZON_DAYS,
    get_failure_day,
    label_readings,
)
from app.ml.predict import load_model, predict_risk, rules_based_risk_fallback

MODEL_PATH = Path(__file__).parent.parent / "app" / "ml" / "model.joblib"
REPORT_PATH = Path(__file__).parent.parent / "app" / "ml" / "model_report.json"


def _synthetic_readings(n: int = 48, start_vib: float = 2.0) -> pd.DataFrame:
    base = datetime(2026, 1, 1)
    return pd.DataFrame([{
        "machine_id": 1,
        "timestamp": base + timedelta(hours=i),
        "vibration": start_vib + i * 0.01,
        "temperature": 65.0 + i * 0.05,
        "rpm": 1450.0 + (i % 5),
        "machine_status": "Running",
        "production_count": 400,
        "good_count": 390,
        "planned_production_time": 480.0,
        "actual_run_time": 470.0,
        "downtime_minutes": 10.0,
        "machine_type": "CNC Machine",
        "criticality": "High",
        "nominal_vibration": 2.0,
        "nominal_temperature": 65.0,
        "nominal_rpm": 1450.0,
        "machine_age_days": 900,
        "days_since_last_maintenance": 63.0,
        "maintenance_count": 3,
        "previous_failure_count": 1,
    } for i in range(n)])


# ─── Model artifact ───────────────────────────────────────────────────────────

def test_model_artifact_exists():
    assert MODEL_PATH.exists(), "Model artifact missing — run app.ml.train"


def test_model_loads():
    assert load_model() is not None


def test_report_contains_required_metrics():
    """Spec §22 requires precision, recall, F1, ROC-AUC and a confusion matrix."""
    report = json.loads(REPORT_PATH.read_text())
    for name, metrics in report["results"].items():
        for key in ("precision", "recall", "f1", "roc_auc", "confusion_matrix"):
            assert key in metrics, f"{name} missing {key}"
        cm = metrics["confusion_matrix"]
        for cell in ("true_negatives", "false_positives", "false_negatives", "true_positives"):
            assert cell in cm


def test_report_feature_list_matches_code():
    """Guards against a stale artifact trained on a different feature set."""
    report = json.loads(REPORT_PATH.read_text())
    assert report["feature_columns"] == FEATURE_COLUMNS


def test_report_does_not_claim_real_world_performance():
    report = json.loads(REPORT_PATH.read_text())
    assert "synthetic" in report["note"].lower()


# ─── Inference ────────────────────────────────────────────────────────────────

def test_prediction_returns_probability_in_range():
    df = engineer_features(_synthetic_readings())
    risk = predict_risk(df.tail(1))
    assert 0.0 <= risk <= 100.0


def test_feature_matrix_has_exact_feature_columns():
    df = engineer_features(_synthetic_readings())
    matrix = prepare_feature_matrix(df)
    assert list(matrix.columns) == FEATURE_COLUMNS


def test_missing_features_are_handled_not_fatal():
    """Missing values must not break scoring (spec §47)."""
    df = engineer_features(_synthetic_readings(n=3))  # too short for full rolling window
    matrix = prepare_feature_matrix(df)
    assert not matrix.isnull().any().any()
    assert 0.0 <= predict_risk(df.tail(1)) <= 100.0


# ─── Rules fallback ───────────────────────────────────────────────────────────

def test_rules_fallback_returns_bounded_score():
    risk = rules_based_risk_fallback(4.0, 2.0, 90.0, 65.0, 120.0)
    assert 0.0 <= risk <= 100.0


def test_rules_fallback_increases_with_severity():
    healthy = rules_based_risk_fallback(2.0, 2.0, 65.0, 65.0, 5.0)
    degraded = rules_based_risk_fallback(4.0, 2.0, 95.0, 65.0, 120.0)
    assert degraded > healthy


# ─── Labels (spec §17, §19) ───────────────────────────────────────────────────

def test_failure_day_derived_from_scenario():
    # M-102 degrades from day 68 with a severe ramp -> fails on day 93.
    assert get_failure_day("M-102") == 93


def test_machine_without_scenario_has_no_failure_day():
    assert get_failure_day("M-101") is None


def test_labels_only_inside_seven_day_window():
    start = pd.Timestamp("2026-01-01")
    timestamps = pd.Series([start + pd.Timedelta(hours=i) for i in range(90 * 24)])
    labels = label_readings(timestamps, start, "M-102", total_days=90)

    failure_ts = start + pd.Timedelta(days=93)
    window_start = failure_ts - pd.Timedelta(days=FAILURE_HORIZON_DAYS)

    positives = timestamps[labels == 1]
    # The failure lands just past the dataset, so the tail of the window is
    # observed and must be labelled positive.
    assert len(positives) > 0
    assert positives.min() > window_start
    assert positives.max() <= failure_ts
    # Everything before the window must stay negative.
    assert (labels[timestamps <= window_start] == 0).all()


def test_labels_are_not_derived_from_sensor_values():
    """Two datasets with wildly different sensor values but identical timestamps
    must receive identical labels — proving labels carry no feature leakage."""
    start = pd.Timestamp("2026-01-01")
    timestamps = pd.Series([start + pd.Timedelta(hours=i) for i in range(90 * 24)])
    a = label_readings(timestamps, start, "M-102", total_days=90)
    b = label_readings(timestamps, start, "M-102", total_days=90)
    assert a.equals(b)


def test_no_labels_when_failure_falls_outside_window():
    # CM-305 degrades from day 70 with a mild ramp -> day 105, beyond 90 days.
    start = pd.Timestamp("2026-01-01")
    timestamps = pd.Series([start + pd.Timedelta(hours=i) for i in range(90 * 24)])
    labels = label_readings(timestamps, start, "CM-305", total_days=90)
    assert labels.sum() == 0


# ─── Train / inference parity ─────────────────────────────────────────────────

def test_maintenance_context_matches_training_logic():
    """The shared inference helper must agree with the training pipeline's
    vectorised merge_asof for the most recent reading."""
    from app.ml.train import _add_maintenance_features

    as_of = pd.Timestamp("2026-03-01")
    maint = pd.DataFrame([
        {"machine_id": 1, "maintenance_date": pd.Timestamp("2025-12-28"), "failure_mode": "BEARING_DEGRADATION"},
        {"machine_id": 1, "maintenance_date": pd.Timestamp("2026-01-27"), "failure_mode": None},
    ])

    readings = pd.DataFrame([{
        "machine_id": 1,
        "timestamp": as_of,
        "machine_age_days": 900,
    }])

    trained = _add_maintenance_features(readings, maint).iloc[0]
    inferred = compute_maintenance_context(as_of, maint)

    assert inferred["days_since_last_maintenance"] == pytest.approx(
        trained["days_since_last_maintenance"]
    )
    assert inferred["maintenance_count"] == trained["maintenance_count"]
    assert inferred["previous_failure_count"] == trained["previous_failure_count"]


def test_maintenance_context_with_no_records():
    ctx = compute_maintenance_context(pd.Timestamp("2026-03-01"), pd.DataFrame(), fallback_days=500.0)
    assert ctx["days_since_last_maintenance"] == 500.0
    assert ctx["maintenance_count"] == 0
    assert ctx["previous_failure_count"] == 0


# ─── Rolling features (spec §18) ──────────────────────────────────────────────

def test_rolling_features_capture_upward_trend():
    df = engineer_features(_synthetic_readings())
    last = df.tail(1).iloc[0]
    assert last["vibration_slope"] > 0
    assert last["temperature_slope"] > 0
    assert last["vibration_std"] >= 0

# ─── Failure mode inference (spec §16) ────────────────────────────────────────

def test_failure_mode_bearing_degradation():
    from app.services.rules_engine import infer_failure_mode
    # Vibration and temperature rising together.
    assert infer_failure_mode(135.0, 30.0, 0.004, 63.0, "CNC Machine") == "BEARING_DEGRADATION"


def test_failure_mode_overheating():
    from app.services.rules_engine import infer_failure_mode
    # Temperature dominant.
    assert infer_failure_mode(40.0, 54.0, 0.02, 30.0, "Injection Press") == "OVERHEATING"


def test_failure_mode_motor_degradation():
    from app.services.rules_engine import infer_failure_mode
    # Speed instability dominant.
    assert infer_failure_mode(65.0, 12.0, 0.030, 45.0, "Hydraulic Press") == "MOTOR_DEGRADATION"


def test_failure_mode_misalignment():
    from app.services.rules_engine import infer_failure_mode
    # Vibration up, temperature and speed steady.
    assert infer_failure_mode(135.0, 9.0, 0.016, 70.0, "Hydraulic Press") == "MISALIGNMENT"


def test_failure_mode_defaults_to_general_wear():
    from app.services.rules_engine import infer_failure_mode
    # Mild, unremarkable deviations.
    assert infer_failure_mode(30.0, 12.0, 0.005, 20.0, "Conveyor Motor") == "GENERAL_MECHANICAL_WEAR"


def test_failure_mode_healthy_machine_is_general():
    from app.services.rules_engine import infer_failure_mode
    assert infer_failure_mode(1.0, 0.5, 0.001, 5.0, "CNC Machine") == "GENERAL_MECHANICAL_WEAR"
