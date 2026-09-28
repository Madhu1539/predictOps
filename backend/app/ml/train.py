"""
ML Training Pipeline — spec §20-21.
Time-based split to prevent temporal leakage.
Trains: Logistic Regression, Random Forest, GradientBoosting.
Saves best model (default: GradientBoostingClassifier).
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pandas as pd
import numpy as np
import joblib
import json
import logging
from pathlib import Path
from datetime import datetime

from sklearn.linear_model import LogisticRegression
from sklearn.ensemble import RandomForestClassifier, GradientBoostingClassifier
from sklearn.calibration import CalibratedClassifierCV
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import Pipeline
from sklearn.metrics import (
    precision_score, recall_score, f1_score,
    roc_auc_score, confusion_matrix, classification_report
)
import sqlalchemy
from sqlalchemy import create_engine, text

from app.ml.feature_engineering import engineer_features, prepare_feature_matrix, FEATURE_COLUMNS, NEUTRAL_DAYS_SINCE_MAINTENANCE
from app.ml.labeling import label_readings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TRAIN_DAYS = 75
TEST_DAYS = 15
# A candidate model must beat this precision to be considered; below it the
# model is effectively alarming on everything (see model selection below).
MIN_USABLE_PRECISION = 0.25
DEFAULT_MODEL_NAME = "Gradient Boosting"  # spec §21 default
# A recall lead smaller than this does not count as "clearly better" (spec §21),
# so the default model keeps the slot.
RECALL_TOLERANCE = 0.03
MODEL_OUTPUT_PATH = Path(__file__).parent / "model.joblib"
REPORT_OUTPUT_PATH = Path(__file__).parent / "model_report.json"


def load_data_from_db(db_url: str) -> pd.DataFrame:
    """Load sensor readings joined with machine and maintenance context."""
    # Convert the async URL to its synchronous equivalent. Done centrally because
    # the previous string replacement handled only SQLite and produced an
    # unloadable URL for Postgres.
    from app.database import sync_database_url

    engine = create_engine(sync_database_url(db_url))

    query = """
    SELECT
        s.id,
        s.machine_id,
        s.timestamp,
        s.vibration,
        s.temperature,
        s.rpm,
        s.machine_status,
        s.production_count,
        s.good_count,
        s.planned_production_time,
        s.actual_run_time,
        s.downtime_minutes,
        m.name AS machine_name,
        m.type AS machine_type,
        m.criticality,
        m.install_date,
        m.nominal_vibration,
        m.nominal_temperature,
        m.nominal_rpm
    FROM sensor_readings s
    JOIN machines m ON s.machine_id = m.id
    ORDER BY s.machine_id, s.timestamp
    """

    df = pd.read_sql(query, engine)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df["install_date"] = pd.to_datetime(df["install_date"])
    df["machine_age_days"] = (df["timestamp"] - df["install_date"]).dt.days

    # Load maintenance context
    maint_query = """
    SELECT
        machine_id,
        maintenance_date,
        failure_mode
    FROM maintenance_records
    ORDER BY machine_id, maintenance_date
    """
    maint_df = pd.read_sql(maint_query, engine)
    maint_df["maintenance_date"] = pd.to_datetime(maint_df["maintenance_date"])

    # For each reading, compute days since last maintenance and maintenance counts
    df = _add_maintenance_features(df, maint_df)

    return df


def _add_maintenance_features(df: pd.DataFrame, maint_df: pd.DataFrame) -> pd.DataFrame:
    """Add maintenance context using vectorized merge_asof per machine."""
    df = df.copy()
    df["days_since_last_maintenance"] = NEUTRAL_DAYS_SINCE_MAINTENANCE
    df["maintenance_count"] = 0
    df["previous_failure_count"] = 0

    # Ensure naive timestamps
    df["timestamp"] = pd.to_datetime(df["timestamp"]).dt.tz_localize(None)
    if not maint_df.empty:
        maint_df = maint_df.copy()
        maint_df["maintenance_date"] = pd.to_datetime(maint_df["maintenance_date"]).dt.tz_localize(None)

    for machine_id in df["machine_id"].unique():
        mask = df["machine_id"] == machine_id
        m_data = df.loc[mask].sort_values("timestamp")
        machine_maint = maint_df[maint_df["machine_id"] == machine_id].sort_values("maintenance_date").copy()

        if machine_maint.empty:
            # No maintenance history at all. Using machine age here taught the model
            # a spurious "very old reading of this feature => safe" rule: 53% of
            # training rows landed in that region with a 0.0 failure rate, so a
            # machine with no records scored 0% risk however bad its sensors were.
            # A neutral in-distribution value keeps the model reasoning from the
            # sensor evidence instead.
            df.loc[mask, "days_since_last_maintenance"] = NEUTRAL_DAYS_SINCE_MAINTENANCE
            continue

        machine_maint["_maint_idx"] = range(len(machine_maint))
        machine_maint["_cum_fail"] = machine_maint["failure_mode"].notna().cumsum()

        merged = pd.merge_asof(
            m_data[["timestamp"]].reset_index(),   # left: (original_index, timestamp)
            machine_maint[["maintenance_date", "_maint_idx", "_cum_fail"]],
            left_on="timestamp",
            right_on="maintenance_date",
            direction="backward",
        )

        # merged has columns: index (original), timestamp, maintenance_date, _maint_idx, _cum_fail
        days = (merged["timestamp"] - merged["maintenance_date"]).dt.days
        # Rows before this machine's first maintenance record: unknown, not ancient.
        days = days.fillna(NEUTRAL_DAYS_SINCE_MAINTENANCE)
        cnt = (merged["_maint_idx"].fillna(-1) + 1).astype(int)
        fail = merged["_cum_fail"].fillna(0).astype(int)

        # Assign back using original index
        orig_idx = merged["index"].values
        df.loc[orig_idx, "days_since_last_maintenance"] = days.values
        df.loc[orig_idx, "maintenance_count"] = cnt.values
        df.loc[orig_idx, "previous_failure_count"] = fail.values

    return df


def create_failure_labels(df: pd.DataFrame) -> pd.DataFrame:
    """
    Target: will_fail_within_7_days (spec §17).

    Labels come from the injected failure event day for each machine, so the
    target is independent of the sensor features. Deriving labels from rolling
    sensor statistics that are also model inputs would be target leakage and
    would make the reported metrics meaningless.
    """
    df = df.copy()

    if "failure_label" in df.columns:
        df["will_fail_within_7_days"] = df["failure_label"].astype(int)
        return df

    df["will_fail_within_7_days"] = 0

    for machine_id, machine_df in df.groupby("machine_id"):
        machine_df = machine_df.sort_values("timestamp")
        machine_name = machine_df["machine_name"].iloc[0] if "machine_name" in machine_df.columns else None
        if machine_name is None:
            continue

        machine_start = machine_df["timestamp"].min()
        total_days = int((machine_df["timestamp"].max() - machine_start).days)

        labels = label_readings(
            machine_df["timestamp"], machine_start, machine_name, total_days
        )
        df.loc[labels.index, "will_fail_within_7_days"] = labels.values

    return df


def time_based_split(df: pd.DataFrame):
    """Time-based split per spec §19: first 75 days train, next 15 days test.

    The test window is bounded at TEST_DAYS so evaluation covers the intended
    15-day horizon rather than an open-ended remainder.
    """
    min_ts = df["timestamp"].min()

    train_cutoff = min_ts + pd.Timedelta(days=TRAIN_DAYS)
    test_cutoff = train_cutoff + pd.Timedelta(days=TEST_DAYS)

    train_df = df[df["timestamp"] <= train_cutoff].copy()
    test_df = df[(df["timestamp"] > train_cutoff) & (df["timestamp"] <= test_cutoff)].copy()

    logger.info(f"Train: {len(train_df)} rows | Test: {len(test_df)} rows")
    logger.info(f"Train window: {min_ts} .. {train_cutoff}")
    logger.info(f"Test window:  {train_cutoff} .. {test_cutoff}")
    logger.info(f"Train failure rate: {train_df['will_fail_within_7_days'].mean():.3f}")
    logger.info(f"Test failure rate: {test_df['will_fail_within_7_days'].mean():.3f}")

    return train_df, test_df


def train(db_url: str = None):
    """Main training function."""
    if db_url is None:
        from app.config import get_settings
        settings = get_settings()
        db_url = settings.database_url

    logger.info("Loading data from database...")
    df = load_data_from_db(db_url)
    logger.info(f"Loaded {len(df)} sensor readings across {df['machine_id'].nunique()} machines")

    # Feature engineering
    logger.info("Engineering features...")
    df = engineer_features(df)
    df = create_failure_labels(df)

    # Time-based split
    train_df, test_df = time_based_split(df)

    X_train = prepare_feature_matrix(train_df)
    y_train = train_df["will_fail_within_7_days"]
    X_test = prepare_feature_matrix(test_df)
    y_test = test_df["will_fail_within_7_days"]

    # ── Train models ────────────────────────────────────────────────────────
    models = {
        "Logistic Regression": Pipeline([
            ("scaler", StandardScaler()),
            ("clf", LogisticRegression(class_weight="balanced", max_iter=1000, random_state=42)),
        ]),
        "Random Forest": Pipeline([
            ("clf", RandomForestClassifier(
                n_estimators=100, class_weight="balanced", random_state=42, n_jobs=-1
            )),
        ]),
        "Gradient Boosting": Pipeline([
            ("scaler", StandardScaler()),
            ("clf", GradientBoostingClassifier(
                n_estimators=100, max_depth=4, learning_rate=0.1, random_state=42
            )),
        ]),
        # Calibration wraps the classifier INSIDE the pipeline so `named_steps`
        # survives and per-prediction attribution can still reach the estimator.
        # Offered as a competing candidate rather than a replacement: spec §22
        # prioritises recall, so calibration has to earn its place rather than
        # being adopted because the probabilities look tidier.
        "Logistic Regression (calibrated)": Pipeline([
            ("scaler", StandardScaler()),
            ("clf", CalibratedClassifierCV(
                LogisticRegression(class_weight="balanced", max_iter=1000, random_state=42),
                method="sigmoid",
                cv=3,
            )),
        ]),
    }

    results = {}

    for name, pipeline in models.items():
        logger.info(f"Training {name}...")
        pipeline.fit(X_train, y_train)

        y_pred = pipeline.predict(X_test)
        y_prob = pipeline.predict_proba(X_test)[:, 1]

        precision = precision_score(y_test, y_pred, zero_division=0)
        recall = recall_score(y_test, y_pred, zero_division=0)
        f1 = f1_score(y_test, y_pred, zero_division=0)
        try:
            roc_auc = roc_auc_score(y_test, y_prob)
        except Exception:
            roc_auc = 0.5

        # Confusion matrix reported per spec §22.
        cm = confusion_matrix(y_test, y_pred, labels=[0, 1])
        tn, fp, fn, tp = (int(v) for v in cm.ravel())

        # Calibration quality. `saturated_fraction` is the share of predictions
        # pinned at the top of the range: a high value means risk scores collapse
        # onto 100 and stop discriminating between machines, which breaks both the
        # §25 risk bands and the ranked machine list.
        brier = float(np.mean((y_prob - y_test.to_numpy()) ** 2))
        saturated = float(np.mean(y_prob >= 0.995))

        results[name] = {
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "f1": round(f1, 4),
            "roc_auc": round(roc_auc, 4),
            "brier_score": round(brier, 4),
            "saturated_fraction": round(saturated, 4),
            "confusion_matrix": {
                "true_negatives": tn,
                "false_positives": fp,
                "false_negatives": fn,
                "true_positives": tp,
            },
        }

        logger.info(
            f"{name}: Precision={precision:.3f}, Recall={recall:.3f}, "
            f"F1={f1:.3f}, ROC-AUC={roc_auc:.3f} Brier={brier:.4f} "
            f"Saturated={saturated:.3f} | TN={tn} FP={fp} FN={fn} TP={tp}"
        )

    # ── Model selection ─────────────────────────────────────────────────────
    # Spec §22 prioritises recall, but a model that flags almost everything has
    # perfect recall and no operational value: it would put every machine in the
    # Critical band and make the alert engine meaningless. So candidates must
    # first clear a minimum usable precision, and among those the highest recall
    # wins.
    #
    # Spec §21 additionally defaults to Gradient Boosting "unless another simple
    # model is clearly better". A recall lead of a fraction of a point is not
    # "clearly better", so the default is only displaced when the margin exceeds
    # RECALL_TOLERANCE. Without this, a 0.02 recall edge could hand the model to a
    # candidate with far worse precision and heavier probability saturation.
    usable = {
        name: metrics for name, metrics in results.items()
        if metrics["precision"] >= MIN_USABLE_PRECISION
    }
    rejected = sorted(set(results) - set(usable))

    if usable:
        best_model_name = max(usable, key=lambda n: usable[n]["recall"])
        default_metrics = usable.get(DEFAULT_MODEL_NAME)
        if default_metrics is not None and best_model_name != DEFAULT_MODEL_NAME:
            challenger = usable[best_model_name]
            recall_lead = challenger["recall"] - default_metrics["recall"]
            # "Clearly better" (spec §21) has to mean better overall, not better on
            # one metric while collapsing another. A challenger that leads recall by
            # a hair while shedding precision produces mostly false alarms, which
            # destroys trust in the alert queue just as surely as missing failures.
            if recall_lead <= RECALL_TOLERANCE or challenger["f1"] < default_metrics["f1"]:
                logger.info(
                    "Keeping spec default %s over %s: recall lead %.3f, "
                    "precision %.3f vs %.3f, F1 %.3f vs %.3f.",
                    DEFAULT_MODEL_NAME, best_model_name, recall_lead,
                    default_metrics["precision"], challenger["precision"],
                    default_metrics["f1"], challenger["f1"],
                )
                best_model_name = DEFAULT_MODEL_NAME
    else:
        # Nothing cleared the bar — fall back to the spec default rather than
        # shipping a degenerate always-positive model.
        best_model_name = DEFAULT_MODEL_NAME

    best_pipeline = models[best_model_name]
    if rejected:
        logger.info(
            "Rejected for precision < %.2f (degenerate alarm-on-everything): %s",
            MIN_USABLE_PRECISION, ", ".join(rejected),
        )

    logger.info(f"Selected model: {best_model_name}")

    # Save model
    MODEL_OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(best_pipeline, MODEL_OUTPUT_PATH)
    logger.info(f"Model saved to {MODEL_OUTPUT_PATH}")

    # Save report
    from app.ml.attribution import global_importance
    from app.ml.guardrails import compute_feature_ranges

    report = {
        "selected_model": best_model_name,
        "selection_rule": (
            f"Highest recall among models with precision >= {MIN_USABLE_PRECISION} "
            f"(spec §22 prioritises recall). The spec §21 default "
            f"'{DEFAULT_MODEL_NAME}' is only displaced when another model leads "
            f"recall by more than {RECALL_TOLERANCE} AND does not have a worse F1, "
            f"since trading a large precision loss for a marginal recall gain is "
            f"not 'clearly better'. Probability calibration is offered as a "
            f"candidate but is rejected when it costs recall."
        ),
        "feature_columns": FEATURE_COLUMNS,
        "train_samples": len(X_train),
        "test_samples": len(X_test),
        "train_failure_rate": float(y_train.mean()),
        "test_failure_rate": float(y_test.mean()),
        "results": results,
        "global_importance": global_importance(best_pipeline),
        # Per-feature ranges the model actually has evidence for. Inference clamps
        # to these, because a tree ensemble given an out-of-range value falls into
        # an arbitrary leaf rather than extrapolating.
        "feature_ranges": compute_feature_ranges(X_train),
        "trained_at": datetime.utcnow().isoformat(),
        "note": "Trained on synthetic data. Metrics demonstrate workflow, not real factory performance.",
    }
    with open(REPORT_OUTPUT_PATH, "w") as f:
        json.dump(report, f, indent=2)

    logger.info("Training complete.")
    return best_pipeline, report


if __name__ == "__main__":
    train()
