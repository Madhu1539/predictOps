"""
Alerts API — GET /api/alerts, GET /api/alerts/{id},
POST /api/alerts/{id}/explain, PATCH /api/alerts/{id},
POST /api/alerts/{id}/workorder
"""
import json
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from typing import List, Optional
from datetime import datetime

from app.database import get_db
from app.models.alert import Alert
from app.models.machine import Machine
from app.models.work_order import WorkOrder
from app.schemas import AlertOut, AlertUpdate, ExplainResponse, WorkOrderOut
from app.llm.explain_service import get_explanation
from app.services.auth_service import require_permission, record_audit
from app.security import limit_llm_requests
from app.services.workorder_service import create_work_order_from_alert

router = APIRouter(prefix="/api/alerts", tags=["alerts"])


def _build_alert_out(alert: Alert, machine: Machine = None) -> AlertOut:
    return AlertOut(
        id=alert.id,
        machine_id=alert.machine_id,
        machine_name=machine.label if machine else None,
        machine_type=machine.type if machine else None,
        risk_score=alert.risk_score,
        severity=alert.severity,
        maintenance_priority=alert.maintenance_priority,
        top_sensors=alert.top_sensors,
        sensor_deviations=alert.sensor_deviations,
        failure_mode=alert.failure_mode,
        days_since_maintenance=alert.days_since_maintenance,
        previous_failure_context=alert.previous_failure_context,
        machine_criticality=alert.machine_criticality,
        production_impact=alert.production_impact,
        part_needed=alert.part_needed,
        part_available=alert.part_available,
        recommended_action=alert.recommended_action,
        status=alert.status,
        created_at=alert.created_at,
        explanation_text=alert.explanation_text,
        degraded_mode=bool(alert.degraded_mode),
        estimated_downtime_cost=alert.estimated_downtime_cost,
        estimated_loss_avoided=alert.estimated_loss_avoided,
        attribution=alert.attribution,
        deviation_score=alert.deviation_score,
        deviation_detail=alert.deviation_detail,
        confidence=alert.confidence,
        confidence_detail=alert.confidence_detail,
    )


@router.get("", response_model=List[AlertOut])
async def list_alerts(
    status: Optional[str] = Query(None),
    severity: Optional[str] = Query(None),
    machine_type: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    # All filters are applied in SQL before LIMIT, otherwise a filtered query
    # can come back empty while matching rows exist beyond the first 100.
    query = select(Alert, Machine).join(
        Machine, Machine.id == Alert.machine_id, isouter=True
    )
    if status:
        query = query.where(Alert.status == status)
    if severity:
        query = query.where(Alert.severity == severity)
    if machine_type:
        query = query.where(Machine.type == machine_type)
    query = query.order_by(Alert.created_at.desc()).limit(100)

    result = await db.execute(query)
    return [_build_alert_out(alert, machine) for alert, machine in result.all()]


@router.get("/{alert_id}", response_model=AlertOut)
async def get_alert(alert_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Alert).where(Alert.id == alert_id))
    alert = result.scalar_one_or_none()
    if not alert:
        raise HTTPException(status_code=404, detail="ALERT_NOT_FOUND")

    mach_result = await db.execute(select(Machine).where(Machine.id == alert.machine_id))
    machine = mach_result.scalar_one_or_none()
    return _build_alert_out(alert, machine)


@router.post(
    "/{alert_id}/explain",
    response_model=ExplainResponse,
    dependencies=[Depends(limit_llm_requests)],
)
async def explain_alert(alert_id: int, db: AsyncSession = Depends(get_db)):
    """Trigger Gemini explanation or deterministic fallback. Cached.

    Rate-limited per client. The cache only covers alerts already explained, so
    repeated calls across distinct alert ids each reach a metered model.
    """
    result = await db.execute(select(Alert).where(Alert.id == alert_id))
    alert = result.scalar_one_or_none()
    if not alert:
        raise HTTPException(status_code=404, detail="ALERT_NOT_FOUND")

    mach_result = await db.execute(select(Machine).where(Machine.id == alert.machine_id))
    machine = mach_result.scalar_one_or_none()

    alert_data = {
        "machine_name": machine.label if machine else f"Machine-{alert.machine_id}",
        "risk_score": alert.risk_score,
        "failure_mode": alert.failure_mode,
        "sensor_deviations": alert.sensor_deviations,
        "days_since_maintenance": alert.days_since_maintenance,
        "previous_failure_context": alert.previous_failure_context,
        "machine_criticality": alert.machine_criticality,
        "production_impact": alert.production_impact,
        "recommended_action": alert.recommended_action,
        "part_available": alert.part_available,
        "attribution": alert.attribution,
        "estimated_loss_avoided": alert.estimated_loss_avoided,
    }

    explanation, source = await get_explanation(alert_id, alert_data)

    # Cache in DB
    if not alert.explanation_cached:
        alert.explanation_text = explanation
        alert.explanation_cached = True
        await db.commit()

    return ExplainResponse(alert_id=alert_id, explanation=explanation, source=source)


@router.patch("/{alert_id}", response_model=AlertOut)
async def update_alert(
    alert_id: int,
    update: AlertUpdate,
    db: AsyncSession = Depends(get_db),
    actor: dict = Depends(require_permission("alert:update")),
):
    """Update alert status: Acknowledged or Dismissed."""
    result = await db.execute(select(Alert).where(Alert.id == alert_id))
    alert = result.scalar_one_or_none()
    if not alert:
        raise HTTPException(status_code=404, detail="ALERT_NOT_FOUND")

    if update.status:
        if update.status not in ("Acknowledged", "Dismissed", "Active"):
            raise HTTPException(status_code=400, detail="Invalid status value")
        previous = alert.status
        alert.status = update.status
        await record_audit(
            db, actor, f"ALERT_{update.status.upper()}", "alert", alert_id,
            f"{previous} -> {update.status}",
        )

    await db.commit()
    await db.refresh(alert)

    mach_result = await db.execute(select(Machine).where(Machine.id == alert.machine_id))
    machine = mach_result.scalar_one_or_none()
    return _build_alert_out(alert, machine)


@router.post("/{alert_id}/workorder", response_model=WorkOrderOut)
async def create_workorder_from_alert(
    alert_id: int,
    db: AsyncSession = Depends(get_db),
    actor: dict = Depends(require_permission("workorder:create")),
):
    """Create work order from alert with auto-populated fields."""
    result = await db.execute(select(Alert).where(Alert.id == alert_id))
    alert = result.scalar_one_or_none()
    if not alert:
        raise HTTPException(status_code=404, detail="ALERT_NOT_FOUND")

    # Check if work order already exists for this alert
    wo_result = await db.execute(
        select(WorkOrder).where(WorkOrder.alert_id == alert_id)
    )
    existing_wo = wo_result.scalar_one_or_none()
    if existing_wo:
        mach_result = await db.execute(select(Machine).where(Machine.id == existing_wo.machine_id))
        machine = mach_result.scalar_one_or_none()
        return WorkOrderOut(
            **{c.name: getattr(existing_wo, c.name) for c in existing_wo.__table__.columns},
            machine_name=machine.label if machine else None,
        )

    mach_result = await db.execute(select(Machine).where(Machine.id == alert.machine_id))
    machine = mach_result.scalar_one_or_none()

    wo = await create_work_order_from_alert(db, alert, machine.label if machine else "")
    await record_audit(
        db, actor, "WORKORDER_CREATED", "work_order", wo.id,
        f"from alert {alert_id} on {machine.name if machine else 'unknown'}",
    )
    await db.commit()
    return WorkOrderOut(
        **{c.name: getattr(wo, c.name) for c in wo.__table__.columns},
        machine_name=machine.label if machine else None,
    )
