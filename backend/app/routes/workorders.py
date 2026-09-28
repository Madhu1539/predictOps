"""
Work Orders API — GET /api/workorders, POST /api/workorders, PATCH /api/workorders/{id}
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from typing import List, Optional
from datetime import datetime, timedelta
from pydantic import BaseModel

from app.database import get_db
from app.models.work_order import WorkOrder
from app.models.alert import Alert
from app.models.machine import Machine
from app.models.technician import Technician
from app.schemas import WorkOrderOut, WorkOrderUpdate
from app.services.auth_service import require_permission, record_audit
from app.services.workorder_service import (
    complete_work_order,
    create_work_order_from_alert,
    get_least_busy_technician,
    get_due_date,
    get_work_order_priority,
)

router = APIRouter(prefix="/api/workorders", tags=["workorders"])


class WorkOrderCreate(BaseModel):
    alert_id: Optional[int] = None
    machine_id: int


def _build_wo_out(wo: WorkOrder, machine_name: str = None) -> WorkOrderOut:
    return WorkOrderOut(
        id=wo.id,
        alert_id=wo.alert_id,
        machine_id=wo.machine_id,
        machine_name=machine_name,
        technician=wo.technician,
        part_needed=wo.part_needed,
        priority=wo.priority,
        due_date=wo.due_date,
        status=wo.status,
        created_at=wo.created_at,
        completed_at=wo.completed_at,
    )


@router.get("", response_model=List[WorkOrderOut])
async def list_work_orders(
    status: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    # Filter in SQL before LIMIT, and join the machine to avoid N+1 lookups.
    query = (
        select(WorkOrder, Machine.name)
        .join(Machine, Machine.id == WorkOrder.machine_id, isouter=True)
    )
    if status:
        query = query.where(WorkOrder.status == status)
    query = query.order_by(WorkOrder.created_at.desc()).limit(100)

    result = await db.execute(query)
    return [_build_wo_out(wo, machine_name) for wo, machine_name in result.all()]


@router.post("", response_model=WorkOrderOut, status_code=201)
async def create_work_order(
    payload: WorkOrderCreate,
    db: AsyncSession = Depends(get_db),
    actor: dict = Depends(require_permission("workorder:create")),
):
    """Create a work order, optionally linked to an alert (auto-fills details).

    Assignment, priority, and due date all come from `workorder_service` so the
    spec §35/§36 rules live in exactly one place.
    """
    # Verify machine exists
    mach_result = await db.execute(select(Machine).where(Machine.id == payload.machine_id))
    machine = mach_result.scalar_one_or_none()
    if not machine:
        raise HTTPException(status_code=404, detail="MACHINE_NOT_FOUND")

    alert = None
    if payload.alert_id:
        alert_result = await db.execute(select(Alert).where(Alert.id == payload.alert_id))
        alert = alert_result.scalar_one_or_none()
        if alert is None:
            raise HTTPException(status_code=404, detail="ALERT_NOT_FOUND")

    if alert is not None:
        wo = await create_work_order_from_alert(db, alert, machine.name)
        return _build_wo_out(wo, machine.name)

    # No alert: derive severity from the machine's latest known risk so the
    # same priority and due-date rules still apply.
    latest_alert_result = await db.execute(
        select(Alert)
        .where(Alert.machine_id == payload.machine_id)
        .order_by(Alert.created_at.desc())
        .limit(1)
    )
    latest = latest_alert_result.scalar_one_or_none()
    severity = latest.severity if latest else "Watch"

    technician = await get_least_busy_technician(db)
    wo = WorkOrder(
        machine_id=payload.machine_id,
        alert_id=None,
        status="Open",
        priority=get_work_order_priority(severity),
        technician=technician,
        part_needed=None,
        due_date=get_due_date(severity),
        created_at=datetime.utcnow(),
    )
    db.add(wo)

    if technician != "Unassigned":
        tech_result = await db.execute(
            select(Technician).where(Technician.name == technician)
        )
        tech = tech_result.scalar_one_or_none()
        if tech:
            tech.active_work_orders += 1

    await db.commit()
    await db.refresh(wo)
    await record_audit(
        db, actor, "WORKORDER_CREATED", "work_order", wo.id,
        f"machine {machine.name}, priority {wo.priority}",
    )
    await db.commit()
    return _build_wo_out(wo, machine.name)


@router.patch("/{wo_id}", response_model=WorkOrderOut)
async def update_work_order(
    wo_id: int,
    update: WorkOrderUpdate,
    db: AsyncSession = Depends(get_db),
    actor: dict = Depends(require_permission("workorder:update")),
):
    """Update work order status: Open → InProgress → Completed."""
    result = await db.execute(select(WorkOrder).where(WorkOrder.id == wo_id))
    wo = result.scalar_one_or_none()
    if not wo:
        raise HTTPException(status_code=404, detail="WORK_ORDER_NOT_FOUND")

    valid_transitions = {
        "Open": ["InProgress", "Completed"],
        "InProgress": ["Completed"],
        "Completed": [],
    }
    allowed = valid_transitions.get(wo.status, [])
    if update.status not in allowed and update.status != wo.status:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid transition: {wo.status} → {update.status}",
        )

    if update.status == "Completed" and wo.status != "Completed":
        previous = wo.status
        wo = await complete_work_order(db, wo)
    else:
        previous = wo.status
        wo.status = update.status
        await db.commit()
        await db.refresh(wo)

    await record_audit(
        db, actor, f"WORKORDER_{update.status.upper()}", "work_order", wo_id,
        f"{previous} -> {update.status}",
    )
    await db.commit()

    mach_result = await db.execute(select(Machine).where(Machine.id == wo.machine_id))
    machine = mach_result.scalar_one_or_none()
    return _build_wo_out(wo, machine.name if machine else None)
