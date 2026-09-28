"""
Backend unit tests — spec §52.
Tests: OEE, risk classification, maintenance priority, alert thresholds,
recommendation rules, feature engineering, spare-part logic.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import pandas as pd
import numpy as np
from datetime import datetime, timedelta


# ─── OEE Tests ────────────────────────────────────────────────────────────────
from app.services.oee_service import calculate_oee, aggregate_plant_oee


def test_oee_full_production():
    # Full production means running at the design rate of 12 units/min
    # (480 min x 12 = 5760 units), which is what IDEAL_CYCLE_TIME encodes.
    result = calculate_oee(
        planned_production_time=480,
        actual_run_time=480,
        production_count=5760,
        good_count=5760,
    )
    assert result["availability"] == 1.0
    assert result["performance"] == pytest.approx(1.0, abs=0.01)
    assert result["quality"] == 1.0
    assert result["oee"] == pytest.approx(1.0, abs=0.01)


def test_oee_with_downtime():
    result = calculate_oee(
        planned_production_time=480,
        actual_run_time=400,
        production_count=4000,
        good_count=3800,
    )
    assert result["availability"] == pytest.approx(400 / 480, abs=0.01)
    assert result["quality"] == pytest.approx(3800 / 4000, abs=0.01)
    assert 0 < result["oee"] < 1.0


def test_oee_zero_planned_time():
    result = calculate_oee(0, 0, 0, 0)
    assert result["oee"] == 0.0


def test_aggregate_plant_oee():
    machines = [{"oee": 0.8}, {"oee": 0.6}, {"oee": 0.9}]
    assert aggregate_plant_oee(machines) == pytest.approx(0.7667, abs=0.01)


def test_aggregate_empty():
    assert aggregate_plant_oee([]) == 0.0


# ─── Risk Classification Tests ─────────────────────────────────────────────────
from app.services.rules_engine import classify_risk, probability_to_risk_score, should_create_alert


def test_classify_critical():
    assert classify_risk(87.0) == "Critical"
    assert classify_risk(75.0) == "Critical"
    assert classify_risk(100.0) == "Critical"


def test_classify_warning():
    assert classify_risk(45.0) == "Warning"
    assert classify_risk(60.0) == "Warning"
    assert classify_risk(74.9) == "Warning"


def test_classify_watch():
    assert classify_risk(25.0) == "Watch"
    assert classify_risk(35.0) == "Watch"
    assert classify_risk(44.9) == "Watch"


def test_classify_normal():
    assert classify_risk(0.0) == "Normal"
    assert classify_risk(10.0) == "Normal"
    assert classify_risk(24.9) == "Normal"


def test_probability_to_risk_score():
    assert probability_to_risk_score(0.87) == 87.0
    assert probability_to_risk_score(0.0) == 0.0
    assert probability_to_risk_score(1.0) == 100.0


def test_alert_creation_critical():
    assert should_create_alert(87.0) is True
    assert should_create_alert(75.0) is True


def test_alert_creation_warning():
    assert should_create_alert(60.0) is True
    assert should_create_alert(45.0) is True


def test_alert_creation_watch_streak():
    assert should_create_alert(35.0, watch_streak=2) is False
    assert should_create_alert(35.0, watch_streak=3) is True
    assert should_create_alert(35.0, watch_streak=5) is True


def test_no_alert_normal():
    assert should_create_alert(10.0) is False
    assert should_create_alert(24.9) is False


# ─── Maintenance Priority Tests ────────────────────────────────────────────────
from app.services.priority_service import calculate_maintenance_priority


def test_priority_high_risk_high_criticality():
    """Machine A example from spec §27: risk=92%, High+High → ~96"""
    priority = calculate_maintenance_priority(92.0, "High", "High")
    assert priority > 85.0  # Should be in high range


def test_priority_high_risk_low_criticality():
    """Machine B example: risk=97%, Low+Low → ~61"""
    priority = calculate_maintenance_priority(97.0, "Low", "Low")
    assert priority < 75.0  # Should be in medium range


def test_priority_normalized():
    """Priority should never exceed 100"""
    priority = calculate_maintenance_priority(100.0, "High", "High")
    assert priority <= 100.0


# ─── Recommendation Tests ──────────────────────────────────────────────────────
from app.services.recommendation_service import get_recommendation, get_required_part


def test_bearing_recommendation():
    rec = get_recommendation("BEARING_DEGRADATION")
    assert "bearing" in rec.lower()


def test_overheating_recommendation():
    rec = get_recommendation("OVERHEATING")
    assert "cooling" in rec.lower()


def test_bearing_part():
    part = get_required_part("BEARING_DEGRADATION")
    assert "Bearing" in part


def test_overheating_part():
    part = get_required_part("OVERHEATING")
    assert "Fan" in part or "Cool" in part


def test_unknown_failure_mode():
    rec = get_recommendation("UNKNOWN_MODE")
    assert len(rec) > 0  # Should return default recommendation


# ─── Feature Engineering Tests ────────────────────────────────────────────────
from app.ml.feature_engineering import engineer_features, prepare_feature_matrix, FEATURE_COLUMNS


def _make_test_df(n=30):
    dates = [datetime.utcnow() - timedelta(hours=n - i) for i in range(n)]
    return pd.DataFrame({
        "machine_id": [1] * n,
        "timestamp": dates,
        "vibration": np.random.uniform(1.5, 2.5, n),
        "temperature": np.random.uniform(60, 70, n),
        "rpm": np.random.uniform(1400, 1500, n),
        "machine_status": ["Running"] * n,
        "production_count": np.random.randint(400, 600, n),
        "good_count": np.random.randint(380, 580, n),
        "planned_production_time": [60.0] * n,
        "actual_run_time": np.random.uniform(50, 60, n),
        "downtime_minutes": np.random.uniform(0, 5, n),
        "machine_type": ["CNC Machine"] * n,
        "criticality": ["High"] * n,
        "machine_age_days": [500] * n,
        "days_since_last_maintenance": [30.0] * n,
        "maintenance_count": [5] * n,
        "previous_failure_count": [1] * n,
        "nominal_vibration": [2.0] * n,
        "nominal_temperature": [65.0] * n,
        "nominal_rpm": [1450.0] * n,
    })


def test_feature_engineering_produces_all_columns():
    df = _make_test_df(30)
    result = engineer_features(df)
    X = prepare_feature_matrix(result)
    assert list(X.columns) == FEATURE_COLUMNS


def test_feature_engineering_no_nulls():
    df = _make_test_df(30)
    result = engineer_features(df)
    X = prepare_feature_matrix(result)
    assert X.isnull().sum().sum() == 0


def test_rolling_features_computed():
    df = _make_test_df(30)
    result = engineer_features(df)
    assert "vibration_mean" in result.columns
    assert "vibration_std" in result.columns
    assert "vibration_slope" in result.columns


def test_criticality_encoding():
    df = _make_test_df(5)
    df["criticality"] = "High"
    result = engineer_features(df)
    assert result["criticality_encoded"].iloc[0] == 2

    df["criticality"] = "Low"
    result2 = engineer_features(df)
    assert result2["criticality_encoded"].iloc[0] == 0


# ─── Work Order Service Tests ──────────────────────────────────────────────────
from app.services.workorder_service import get_due_date, get_work_order_priority


def test_urgent_due_date():
    due = get_due_date("Critical")
    diff = (due - datetime.utcnow()).days
    assert diff <= 1


def test_warning_due_date():
    due = get_due_date("Warning")
    diff = (due - datetime.utcnow()).days
    assert diff <= 7


def test_priority_mapping():
    assert get_work_order_priority("Critical") == "Urgent"
    assert get_work_order_priority("Warning") == "Medium"
    assert get_work_order_priority("Watch") == "Low"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
