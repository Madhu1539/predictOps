"""
Shared feature engineering module.
CRITICAL: Training and inference must use exactly the same logic.
"""
import pandas as pd
import numpy as np
from typing import List, Optional

# Neutral stand-in for `days_since_last_maintenance` when no record exists.
#
# Measured from the training data: machines with maintenance history span 9-71
# days, median 29, mean 33.1. The former 999-day sentinel sat 51.5 standard
# deviations above that mean, forcing the model to extrapolate far outside its
# evidence and systematically inflating risk for any machine whose maintenance
# records were simply absent.
NEUTRAL_DAYS_SINCE_MAINTENANCE = 29.0

# ─── Feature column names (single source of truth) ───────────────────────────
FEATURE_COLUMNS = [
    # Raw sensor
    "vibration", "temperature", "rpm",
    # Rolling features (24-reading window)
    "vibration_mean", "vibration_std", "vibration_slope",
    "temperature_mean", "temperature_std", "temperature_slope",
    "rpm_mean", "rpm_std", "rpm_slope",
    # Maintenance features
    "days_since_last_maintenance", "maintenance_count", "previous_failure_count",
    # Machine context
    "machine_age_days", "criticality_encoded",
    "machine_type_CNC Machine", "machine_type_Conveyor Motor",
    "machine_type_Hydraulic Press", "machine_type_Industrial Pump",
    # Production context
    "recent_downtime", "production_count", "machine_availability",
]

WINDOW = 24  # number of readings for rolling stats


def _compute_slope(series: pd.Series) -> float:
    """Linear slope of a series (least squares)."""
    if len(series) < 2:
        return 0.0
    x = np.arange(len(series), dtype=float)
    y = series.values.astype(float)
    if np.std(x) == 0:
        return 0.0
    return float(np.polyfit(x, y, 1)[0])


def engineer_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Accepts a DataFrame with columns:
        machine_id, timestamp, vibration, temperature, rpm,
        machine_status, production_count, good_count,
        planned_production_time, actual_run_time, downtime_minutes,
        days_since_last_maintenance, maintenance_count,
        previous_failure_count, machine_age_days, criticality,
        machine_type
    Returns a DataFrame with FEATURE_COLUMNS ready for the model.
    """
    df = df.copy()
    df = df.sort_values(["machine_id", "timestamp"]).reset_index(drop=True)

    # ── Rolling features per machine ─────────────────────────────────────────
    for col in ["vibration", "temperature", "rpm"]:
        grp = df.groupby("machine_id")[col]
        df[f"{col}_mean"] = grp.transform(
            lambda s: s.rolling(WINDOW, min_periods=1).mean()
        )
        df[f"{col}_std"] = grp.transform(
            lambda s: s.rolling(WINDOW, min_periods=1).std().fillna(0.0)
        )
        # slope: computed on windows — approximate via expanding for each row
        df[f"{col}_slope"] = grp.transform(
            lambda s: s.rolling(WINDOW, min_periods=2).apply(_compute_slope, raw=False).fillna(0.0)
        )

    # ── Production context ────────────────────────────────────────────────────
    df["recent_downtime"] = df.groupby("machine_id")["downtime_minutes"].transform(
        lambda s: s.rolling(WINDOW, min_periods=1).sum()
    )

    # machine_availability = actual_run_time / planned_production_time
    df["machine_availability"] = (
        df["actual_run_time"] / df["planned_production_time"].replace(0, 1)
    ).clip(0, 1)

    # ── Categorical encoding ──────────────────────────────────────────────────
    criticality_map = {"Low": 0, "Medium": 1, "High": 2}
    df["criticality_encoded"] = df["criticality"].map(criticality_map).fillna(1)

    machine_type_dummies = pd.get_dummies(df["machine_type"], prefix="machine_type")
    for col in [
        "machine_type_CNC Machine",
        "machine_type_Conveyor Motor",
        "machine_type_Hydraulic Press",
        "machine_type_Industrial Pump",
    ]:
        if col not in machine_type_dummies.columns:
            machine_type_dummies[col] = 0
    df = pd.concat([df, machine_type_dummies], axis=1)

    # ── Ensure all feature columns exist ─────────────────────────────────────
    for col in FEATURE_COLUMNS:
        if col not in df.columns:
            df[col] = 0.0

    return df


def prepare_feature_matrix(df: pd.DataFrame) -> pd.DataFrame:
    """Return only the feature columns, in order."""
    return df[FEATURE_COLUMNS].fillna(0.0)


def compute_maintenance_context(
    as_of: pd.Timestamp,
    maintenance_df: pd.DataFrame,
    fallback_days: Optional[float] = None,
) -> dict:
    """Maintenance features as of a point in time (spec §18).

    Shared by inference so that `days_since_last_maintenance`,
    `maintenance_count`, and `previous_failure_count` are computed in exactly
    one place. The training pipeline derives the same three values row-wise via
    a vectorised merge_asof; `tests/test_ml.py` asserts the two agree.

    When no maintenance record exists, the honest answer is "unknown", not
    "overdue". The previous 999-day sentinel sat 51.5 standard deviations above
    the training mean (the model only ever saw 9-71 days, median 29), so it forced
    the model to extrapolate far outside its evidence and inflated risk. Two
    genuinely different situations cannot be distinguished here:

        - the machine has never been maintained
        - the maintenance records were simply not supplied

    The second is overwhelmingly more common, so the neutral in-distribution value
    is used and `maintenance_context_available` reports that the feature was
    neutralised rather than measured.
    """
    neutral = NEUTRAL_DAYS_SINCE_MAINTENANCE if fallback_days is None else fallback_days
    empty = {
        "days_since_last_maintenance": neutral,
        "maintenance_count": 0,
        "previous_failure_count": 0,
        "maintenance_context_available": False,
    }
    if maintenance_df is None or maintenance_df.empty:
        return empty

    maint = maintenance_df.copy()
    maint["maintenance_date"] = pd.to_datetime(maint["maintenance_date"])
    past = maint[maint["maintenance_date"] <= as_of]
    if past.empty:
        return empty

    last_maint = past["maintenance_date"].max()
    failures = (
        int(past["failure_mode"].notna().sum())
        if "failure_mode" in past.columns
        else 0
    )
    return {
        "days_since_last_maintenance": float((as_of - last_maint).days),
        "maintenance_count": int(len(past)),
        "previous_failure_count": failures,
        "maintenance_context_available": True,
    }
