"""
Inference guardrails — keep predictions inside the model's evidence.

Tree ensembles cannot extrapolate. Given a feature value beyond anything seen in
training they do not "keep trending upward"; they fall into whichever leaf the
last split happens to lead to, which can be an arbitrarily low-risk one.

This guard clamps each feature to the range observed during training and reports
which features were clamped, so an out-of-range reading is bounded rather than
extrapolated, and the exceedance is visible.

What this guard does NOT fix, measured on the shipped model (Conveyor Motor,
training temperature range 49.5-95.2 C):

    temperature 78-90 C  ->  ~98% risk
    temperature 95 C     ->  ~0.1% risk      <-- INSIDE the training range

That collapse is a genuine non-monotonicity of the fitted model, not
extrapolation, so clamping cannot repair it. It is the reason
`anomaly_service.deviation_score` exists as an independent, monotonic signal, and
why `detect_signal_disagreement` flags cases where the two disagree. Relying on
the model alone would mean silently reporting an overheating machine as safe.
"""
import json
import logging
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd

logger = logging.getLogger(__name__)

REPORT_PATH = Path(__file__).resolve().parent / "model_report.json"

# Cached so the report is not re-read on every prediction.
_ranges_cache: Optional[Dict[str, Dict[str, float]]] = None
_ranges_loaded = False


def load_feature_ranges() -> Optional[Dict[str, Dict[str, float]]]:
    """Per-feature training ranges from the model report, or None if absent."""
    global _ranges_cache, _ranges_loaded
    if _ranges_loaded:
        return _ranges_cache

    _ranges_loaded = True
    try:
        with open(REPORT_PATH) as handle:
            report = json.load(handle)
        ranges = report.get("feature_ranges")
        if isinstance(ranges, dict) and ranges:
            _ranges_cache = ranges
        else:
            logger.info("Model report has no feature_ranges; clamping disabled.")
    except FileNotFoundError:
        logger.info("No model report found; clamping disabled.")
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(f"Could not load feature ranges: {exc}")
    return _ranges_cache


def reset_cache() -> None:
    """Drop the cache so a freshly written report is picked up (used by tests)."""
    global _ranges_cache, _ranges_loaded
    _ranges_cache = None
    _ranges_loaded = False


def clamp_features(X: pd.DataFrame) -> Tuple[pd.DataFrame, List[dict]]:
    """Clamp a feature matrix to the training range.

    Returns `(clamped_frame, exceedances)` where each exceedance records the
    feature, the submitted value, the bound applied and the direction. An empty
    list means every value was already inside the model's evidence.

    Never raises: losing the guard must not break scoring, though it is logged.
    """
    ranges = load_feature_ranges()
    if not ranges or X.empty:
        return X, []

    clamped = X.copy()
    exceedances: List[dict] = []

    for column in clamped.columns:
        bounds = ranges.get(column)
        if not bounds:
            continue
        low, high = bounds.get("min"), bounds.get("max")
        if low is None or high is None:
            continue

        value = float(clamped.iloc[0][column])
        if value > high:
            exceedances.append({
                "feature": column, "value": round(value, 4),
                "clamped_to": round(high, 4), "direction": "above_training_range",
            })
            clamped.iloc[0, clamped.columns.get_loc(column)] = high
        elif value < low:
            exceedances.append({
                "feature": column, "value": round(value, 4),
                "clamped_to": round(low, 4), "direction": "below_training_range",
            })
            clamped.iloc[0, clamped.columns.get_loc(column)] = low

    if exceedances:
        logger.debug(f"Clamped {len(exceedances)} feature(s) to the training range")
    return clamped, exceedances


def compute_feature_ranges(X: pd.DataFrame, quantile: float = 0.0) -> Dict[str, Dict[str, float]]:
    """Per-feature training ranges, written into the model report at train time.

    Defaults to the true observed min/max. That is the honest definition of "what
    the model has evidence for": inside the range the tree has real splits, and
    only beyond it does it stop being able to reason.

    Tightening this to inner quantiles was tried and rejected. It flagged
    legitimate in-range values as exceedances, and worse, it left a band between
    the quantile bound and the true maximum where values were passed through
    unclamped and the out-of-range collapse reappeared.
    """
    ranges: Dict[str, Dict[str, float]] = {}
    for column in X.columns:
        # Cast before quantiling: the `machine_type_*` one-hot columns are boolean,
        # and numpy cannot interpolate a quantile on booleans.
        series = pd.to_numeric(X[column], errors="coerce").astype(float).dropna()
        if series.empty:
            continue
        ranges[column] = {
            "min": float(series.quantile(quantile)),
            "max": float(series.quantile(1.0 - quantile)),
        }
    return ranges
