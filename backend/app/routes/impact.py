"""
Business Impact API — GET /api/impact

Expresses risk and downtime in money. Every figure here is a MODELLED estimate
built from ERP cost-centre rates plus the documented assumptions in
`cost_service.ASSUMPTIONS`; none of it is a measured saving. The endpoint returns
those assumptions alongside the numbers so a reader can audit them.
"""
from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from app.database import get_db
from app.models.alert import Alert
from app.models.cost_center import CostCenter
from app.models.machine import Machine
from app.models.material import Material
from app.models.sensor import SensorReading
from app.models.work_order import WorkOrder
from app.schemas import ImpactOut, MachineExposure
from app.services import cost_service
from app.services.rules_engine import classify_risk

router = APIRouter(prefix="/api/impact", tags=["impact"])

OPEN_ALERT_STATUSES = ("Active", "Acknowledged")


@router.get("", response_model=ImpactOut)
async def get_impact(
    window_days: int = Query(30, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
):
    """Plant-level value at risk, value protected, and downtime cost incurred."""
    since = datetime.utcnow() - timedelta(days=window_days)

    cost_centers = {
        cc.code: cc for cc in (await db.execute(select(CostCenter))).scalars().all()
    }
    materials = {
        m.part_name: m for m in (await db.execute(select(Material))).scalars().all()
    }

    # ── Value at risk: open alerts, expected cost of running to failure ──
    open_rows = (
        await db.execute(
            select(Alert, Machine)
            .join(Machine, Machine.id == Alert.machine_id, isouter=True)
            .where(Alert.status.in_(OPEN_ALERT_STATUSES))
        )
    ).all()

    value_at_risk = 0.0
    exposures: List[MachineExposure] = []

    for alert, machine in open_rows:
        cc = cost_centers.get(machine.cost_center_code) if machine else None
        material = materials.get(alert.part_needed) if alert.part_needed else None

        probability = max(0.0, min(alert.risk_score or 0.0, 100.0)) / 100.0
        unplanned = cost_service.estimate_unplanned_failure_cost(alert.failure_mode, cc)
        exposure = round(probability * unplanned, 2)
        value_at_risk += exposure

        avoided = alert.estimated_loss_avoided
        if avoided is None:
            # Alert predates the cost model; compute it now rather than showing a gap.
            avoided = cost_service.estimate_loss_avoided(
                alert.risk_score, alert.failure_mode, cc, material
            )

        if machine is not None:
            exposures.append(
                MachineExposure(
                    machine_id=machine.id,
                    machine_name=machine.label,
                    cost_center_code=machine.cost_center_code,
                    risk_score=round(alert.risk_score or 0.0, 1),
                    severity=alert.severity or classify_risk(alert.risk_score or 0.0),
                    downtime_cost_per_hour=cost_service.get_downtime_cost_per_hour(cc),
                    value_at_risk=exposure,
                    loss_avoided_if_actioned=round(avoided, 2),
                )
            )

    exposures.sort(key=lambda e: e.value_at_risk, reverse=True)

    # ── Value protected: work orders completed inside the window ──
    completed_rows = (
        await db.execute(
            select(WorkOrder, Alert, Machine)
            .join(Alert, Alert.id == WorkOrder.alert_id, isouter=True)
            .join(Machine, Machine.id == WorkOrder.machine_id, isouter=True)
            .where(WorkOrder.status == "Completed")
            .where(WorkOrder.completed_at >= since)
        )
    ).all()

    value_protected = 0.0
    for _wo, alert, machine in completed_rows:
        if alert is None:
            continue
        if alert.estimated_loss_avoided is not None:
            value_protected += alert.estimated_loss_avoided
            continue
        cc = cost_centers.get(machine.cost_center_code) if machine else None
        material = materials.get(alert.part_needed) if alert.part_needed else None
        value_protected += cost_service.estimate_loss_avoided(
            alert.risk_score, alert.failure_mode, cc, material
        )

    # ── Downtime cost actually incurred, from recorded downtime ──
    downtime_rows = (
        await db.execute(
            select(Machine.cost_center_code, func.sum(SensorReading.downtime_minutes))
            .join(Machine, Machine.id == SensorReading.machine_id)
            .where(SensorReading.timestamp >= since)
            .group_by(Machine.cost_center_code)
        )
    ).all()

    downtime_cost_incurred = 0.0
    for code, minutes in downtime_rows:
        downtime_cost_incurred += cost_service.estimate_downtime_cost(
            minutes or 0.0, cost_centers.get(code)
        )

    currency = "USD"
    if cost_centers:
        currency = cost_service.get_currency(next(iter(cost_centers.values())))

    return ImpactOut(
        currency=currency,
        value_at_risk=round(value_at_risk, 2),
        value_protected=round(value_protected, 2),
        downtime_cost_incurred=round(downtime_cost_incurred, 2),
        window_days=window_days,
        open_alert_count=len(open_rows),
        completed_work_order_count=len(completed_rows),
        top_exposure=exposures[:10],
        assumptions=cost_service.ASSUMPTIONS,
        basis=cost_service.ASSUMPTIONS["basis"],
    )
