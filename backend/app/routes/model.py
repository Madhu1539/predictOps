"""
Model transparency API — GET /api/model

Publishes what the model is, how it was chosen, how it scored, and which features
drive it. A risk score that cannot be interrogated is not trustworthy, and the
selection trade-offs (recall over precision, calibration rejected because it cost
recall) are part of the honest story rather than something to hide.
"""
import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter

from app.config import get_settings
from app.ml.attribution import global_importance
from app.services.rules_engine import RISK_BANDS

logger = logging.getLogger(__name__)
settings = get_settings()

router = APIRouter(prefix="/api/model", tags=["model"])

REPORT_PATH = Path(__file__).resolve().parents[1] / "ml" / "model_report.json"


def _load_report() -> Optional[Dict[str, Any]]:
    try:
        with open(REPORT_PATH) as handle:
            return json.load(handle)
    except FileNotFoundError:
        return None
    except Exception as exc:
        logger.warning(f"Could not read model report: {exc}")
        return None


@router.get("")
async def get_model_info():
    """Model card: identity, metrics, selection rule and feature importance."""
    report = _load_report()

    # Importance is read live from the loaded model, so it describes the model
    # actually serving traffic rather than whatever was last written to disk.
    importance = global_importance()
    if importance is None and report:
        importance = report.get("global_importance")

    model_loaded = True
    try:
        from app.ml.predict import get_model
        get_model()
    except Exception:
        model_loaded = False

    if report is None:
        return {
            "model_loaded": model_loaded,
            "available": False,
            "note": "No training report found. Run `python -m app.ml.train`.",
            "risk_bands": RISK_BANDS,
        }

    selected = report.get("selected_model")
    results = report.get("results", {})

    return {
        "available": True,
        "model_loaded": model_loaded,
        "selected_model": selected,
        "selection_rule": report.get("selection_rule"),
        "trained_at": report.get("trained_at"),
        "train_samples": report.get("train_samples"),
        "test_samples": report.get("test_samples"),
        "train_failure_rate": report.get("train_failure_rate"),
        "test_failure_rate": report.get("test_failure_rate"),
        "selected_metrics": results.get(selected),
        "all_candidates": results,
        "feature_count": len(report.get("feature_columns") or []),
        "feature_columns": report.get("feature_columns"),
        "global_importance": (importance or [])[:15],
        "risk_bands": RISK_BANDS,
        "note": report.get("note"),
        "degraded_mode_available": True,
        "degraded_mode_note": (
            "If the model cannot load, scoring falls back to a deterministic rules "
            "engine and every affected alert is flagged degraded_mode=true."
        ),
    }
