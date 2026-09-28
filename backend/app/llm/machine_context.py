"""
Machine and fleet context assembly for the Investigation tab.

One place that answers "everything currently known about this machine", so an
intent handler selects from a single structure instead of issuing its own queries
and inevitably omitting a signal.

That omission was a real defect: the per-intent retrievers predate the deviation
score, the absolute published limits, the confidence assessment and sensor
coverage, so the Investigation tab could report

    "M-102 is scored at 0% failure risk over the next 7 days (Critical)"

without mentioning that deviation and ISO 10816 limits were what raised the alert.
The answer looked broken because the evidence handed to it genuinely was incomplete.
"""
import json
import logging
from typing import Any, Dict, List, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.alert import Alert
from app.models.dataset import Dataset
from app.models.machine import ORIGIN_SIMULATED, Machine
from app.models.maintenance import MaintenanceRecord
from app.models.oee import OeeSnapshot
from app.models.sensor import SensorReading
from app.services.anomaly_service import (
    absolute_concerns,
    absolute_score,
    assess_confidence,
    detect_signal_disagreement,
    deviation_score,
)
from app.services.baseline_service import reference_for

logger = logging.getLogger(__name__)

OPEN_STATUSES = ("Active", "Acknowledged")
SENSORS = ("vibration", "temperature", "rpm")

# Readings used for the short-term trend. One per hour, so roughly two days.
TREND_WINDOW = 48


def _loads(raw: Optional[str], default):
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


async def machine_context(db: AsyncSession, machine: Machine) -> Dict[str, Any]:
    """Everything known about one machine, with each figure's basis attached.

    Deliberately verbose: an answer can omit a field, but it cannot cite one that
    was never retrieved.
    """
    latest = (
        await db.execute(
            select(SensorReading)
            .where(SensorReading.machine_id == machine.id)
            .order_by(SensorReading.timestamp.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    reading_count = (
        await db.execute(
            select(func.count()).select_from(SensorReading)
            .where(SensorReading.machine_id == machine.id)
        )
    ).scalar_one()

    first_reading = (
        await db.execute(
            select(func.min(SensorReading.timestamp))
            .where(SensorReading.machine_id == machine.id)
        )
    ).scalar_one()

    alert = (
        await db.execute(
            select(Alert)
            .where(Alert.machine_id == machine.id)
            .where(Alert.status.in_(OPEN_STATUSES))
            .order_by(Alert.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    # The most recent scored reading is the truth for "risk right now"; an alert can
    # hold an older score.
    scored = (
        await db.execute(
            select(SensorReading.risk_score)
            .where(SensorReading.machine_id == machine.id)
            .where(SensorReading.risk_score.isnot(None))
            .order_by(SensorReading.timestamp.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    ml_risk = round(float(scored), 1) if scored is not None else (
        alert.risk_score if alert else None
    )

    maintenance = (
        await db.execute(
            select(MaintenanceRecord)
            .where(MaintenanceRecord.machine_id == machine.id)
            .order_by(MaintenanceRecord.maintenance_date.desc())
        )
    ).scalars().all()

    snapshot = (
        await db.execute(
            select(OeeSnapshot)
            .where(OeeSnapshot.machine_id == machine.id)
            .order_by(OeeSnapshot.timestamp.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    current = {
        sensor: getattr(latest, sensor, None) if latest else None
        for sensor in SENSORS
    }
    present_sensors = [s for s, v in current.items() if v is not None]
    missing_sensors = [s for s, v in current.items() if v is None]

    reference = reference_for(machine)
    deviation = deviation_score(current, {
        "vibration": reference["vibration"],
        "temperature": reference["temperature"],
        "rpm": reference["rpm"],
    }) if latest else {"score": None, "components": [], "dominant_sensor": None}

    concerns = absolute_concerns(current) if latest else []
    confidence = assess_confidence(
        reading_count=int(reading_count or 0),
        machine_type=machine.type,
        reference_source=reference["source"],
        maintenance_available=bool(maintenance),
        missing_sensors=missing_sensors or None,
    )

    disagreement = (
        detect_signal_disagreement(ml_risk, deviation.get("score") or 0.0)
        if ml_risk is not None else None
    )

    # Short-term direction, computed from stored readings rather than asserted.
    trend = await _sensor_trend(db, machine.id)

    return {
        "machine": machine.label,
        "machine_id": machine.id,
        "type": machine.type,
        "location": machine.location,
        "criticality": machine.criticality,
        "install_date": machine.install_date,
        # Provenance: whether the simulator owns this machine or it was supplied.
        "data_origin": machine.data_origin,
        "is_uploaded_data": machine.data_origin != ORIGIN_SIMULATED,
        "dataset_id": machine.dataset_id,

        # ── Signal 1: the model ───────────────────────────────────────────────
        "ml_risk_score": ml_risk,
        "ml_available": confidence.get("ml_available", True),
        "severity": alert.severity if alert else None,
        "has_open_alert": alert is not None,
        "failure_mode": alert.failure_mode if alert else None,
        "recommended_action": alert.recommended_action if alert else None,
        "top_factors": _loads(alert.attribution, [])[:5] if alert else [],

        # ── Signal 2: model-free deviation ────────────────────────────────────
        "deviation_score": deviation.get("score"),
        "deviation_components": deviation.get("components", []),
        "dominant_sensor": deviation.get("dominant_sensor"),

        # ── Signal 3: absolute published limits ───────────────────────────────
        "absolute_concerns": concerns,
        "absolute_score": absolute_score(concerns),

        # Where the signals disagree, and which to trust.
        "signal_disagreement": disagreement,

        # ── Evidence quality ──────────────────────────────────────────────────
        "confidence": confidence["confidence"],
        "confidence_score": confidence["confidence_score"],
        "confidence_reasons": confidence["reasons"],
        "scoring_basis": confidence["scoring_basis"],

        # ── Measurements and their reference ──────────────────────────────────
        "current_readings": current,
        "present_sensors": present_sensors,
        "missing_sensors": missing_sensors,
        "reference_values": {
            "vibration": reference["vibration"],
            "temperature": reference["temperature"],
            "rpm": reference["rpm"],
        },
        "reference_source": reference["source"],
        "nominal_values": {
            "vibration": machine.nominal_vibration,
            "temperature": machine.nominal_temperature,
            "rpm": machine.nominal_rpm,
        },
        "baseline_method": machine.baseline_method,
        "baseline_sample_count": machine.baseline_sample_count,
        "has_design_spec": bool(machine.has_design_spec),

        # ── History ───────────────────────────────────────────────────────────
        "reading_count": int(reading_count or 0),
        "first_reading": str(first_reading) if first_reading else None,
        "last_reading": str(latest.timestamp) if latest else None,
        "maintenance_record_count": len(maintenance),
        "last_maintenance_date": (
            str(maintenance[0].maintenance_date) if maintenance else None
        ),
        "days_since_maintenance": alert.days_since_maintenance if alert else None,
        "past_failure_modes": [
            m.failure_mode for m in maintenance if m.failure_mode
        ][:5],
        "trend": trend,
        "oee": round(snapshot.oee, 4) if snapshot else None,
    }


async def _sensor_trend(db: AsyncSession, machine_id: int) -> Dict[str, Any]:
    """Direction of each channel over the recent window.

    Compares the mean of the older half with the newer half, which is robust to
    single-point noise in a way that first-vs-last is not.
    """
    rows = (
        await db.execute(
            select(
                SensorReading.vibration,
                SensorReading.temperature,
                SensorReading.rpm,
            )
            .where(SensorReading.machine_id == machine_id)
            .order_by(SensorReading.timestamp.desc())
            .limit(TREND_WINDOW)
        )
    ).all()

    if len(rows) < 6:
        return {"available": False, "reason": f"only {len(rows)} readings in window"}

    ordered = list(reversed(rows))  # oldest first
    midpoint = len(ordered) // 2
    result: Dict[str, Any] = {"available": True, "window_readings": len(ordered)}

    for index, sensor in enumerate(SENSORS):
        older = [r[index] for r in ordered[:midpoint] if r[index] is not None]
        newer = [r[index] for r in ordered[midpoint:] if r[index] is not None]
        if not older or not newer:
            result[sensor] = None  # not instrumented
            continue
        first_mean = sum(older) / len(older)
        second_mean = sum(newer) / len(newer)
        change = second_mean - first_mean
        pct = (change / abs(first_mean) * 100) if first_mean else 0.0
        result[sensor] = {
            "earlier_mean": round(first_mean, 3),
            "recent_mean": round(second_mean, 3),
            "change_pct": round(pct, 1),
            "direction": "rising" if pct > 2 else "falling" if pct < -2 else "flat",
        }
    return result


async def fleet_context(db: AsyncSession, dataset_id: Optional[int] = None) -> Dict[str, Any]:
    """Fleet-level aggregates, scoped to the demo fleet or one uploaded dataset."""
    query = select(Machine)
    query = query.where(
        Machine.dataset_id.is_(None) if dataset_id is None else Machine.dataset_id == dataset_id
    )
    machines = (await db.execute(query)).scalars().all()

    alerts = (
        await db.execute(
            select(Alert).where(Alert.status.in_(OPEN_STATUSES))
        )
    ).scalars().all()
    machine_ids = {m.id for m in machines}
    alerts = [a for a in alerts if a.machine_id in machine_ids]

    by_severity: Dict[str, int] = {}
    for alert in alerts:
        by_severity[alert.severity] = by_severity.get(alert.severity, 0) + 1

    ranked: List[Dict[str, Any]] = []
    for machine in machines:
        alert = next((a for a in alerts if a.machine_id == machine.id), None)
        scored = (
            await db.execute(
                select(SensorReading.risk_score)
                .where(SensorReading.machine_id == machine.id)
                .where(SensorReading.risk_score.isnot(None))
                .order_by(SensorReading.timestamp.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        ranked.append({
            "machine": machine.label,
            "machine_id": machine.id,
            "type": machine.type,
            "criticality": machine.criticality,
            "ml_risk_score": round(float(scored), 1) if scored is not None else None,
            "deviation_score": alert.deviation_score if alert else None,
            "severity": alert.severity if alert else "Normal",
            "confidence": alert.confidence if alert else None,
        })

    # Primary key is the stronger of the two signals, so a machine flagged by
    # deviation alone still ranks. Ties are common and were previously broken by
    # insertion order, which meant three machines pinned at deviation 100 ranked by
    # machine id: the "highest risk" machine could carry 7% ML risk while a tied one
    # carried 78%. Secondary and tertiary keys make the order deterministic and
    # defensible instead of incidental.
    ranked.sort(
        key=lambda r: (
            max(r["ml_risk_score"] or 0.0, r["deviation_score"] or 0.0),
            r["ml_risk_score"] or 0.0,
            r["deviation_score"] or 0.0,
        ),
        reverse=True,
    )

    datasets = (await db.execute(select(Dataset))).scalars().all()

    return {
        "scope": "demo_fleet" if dataset_id is None else f"dataset_{dataset_id}",
        "machine_count": len(machines),
        "open_alerts": len(alerts),
        "alerts_by_severity": by_severity,
        "ranked_machines": ranked,
        "highest_risk": ranked[0] if ranked else None,
        "uploaded_datasets": [
            {
                "id": d.id, "name": d.name, "status": d.status,
                "machine_count": d.machine_count, "row_count": d.row_count,
            }
            for d in datasets
        ],
    }
