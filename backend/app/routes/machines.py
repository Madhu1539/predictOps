"""
Machines API — GET /api/machines, GET /api/machines/{id}
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from typing import List, Optional
import pandas as pd
from datetime import datetime

from app.database import get_db
from app.models.machine import Machine
from app.models.sensor import SensorReading
from app.models.maintenance import MaintenanceRecord
from app.models.alert import Alert
from app.models.dataset import Dataset
from app.models.machine import ORIGIN_EXTERNAL, ORIGIN_SIMULATED
from app.schemas import MachineListItem, MachineDetailOut, SensorReadingOut, MaintenanceRecordOut, RiskHistoryPoint, AlertSummary, MachineCreate
from app.services.auth_service import record_audit, require_permission
from app.services.dataset_service import scoped_name as _scoped_name
from app.services.rules_engine import classify_risk
from app.services.priority_service import calculate_maintenance_priority, get_production_impact

router = APIRouter(prefix="/api/machines", tags=["machines"])


@router.post("", response_model=MachineListItem, status_code=201)
async def create_machine(
    payload: MachineCreate,
    db: AsyncSession = Depends(get_db),
    actor: dict = Depends(require_permission("reading:ingest")),
):
    """Register a machine so readings can be attached to it.

    Nominals are optional. When they are omitted the machine is flagged as having
    no design spec, and `baseline_service` derives an observed reference from the
    machine's own history instead of assuming plant defaults.
    """
    dataset_id = payload.dataset_id
    if dataset_id is not None:
        dataset = (
            await db.execute(select(Dataset).where(Dataset.id == dataset_id))
        ).scalar_one_or_none()
        if dataset is None:
            raise HTTPException(status_code=404, detail="DATASET_NOT_FOUND")

    stored_name = _scoped_name(payload.name, dataset_id)
    existing = (
        await db.execute(select(Machine).where(Machine.name == stored_name))
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail="MACHINE_ALREADY_EXISTS")

    has_spec = any(
        v is not None
        for v in (payload.nominal_vibration, payload.nominal_temperature, payload.nominal_rpm)
    )

    machine = Machine(
        name=stored_name,
        display_name=payload.name,
        type=payload.type,
        location=payload.location,
        criticality=payload.criticality,
        install_date=payload.install_date or datetime.utcnow().strftime("%Y-%m-%d"),
        # Column defaults still apply when a nominal is absent; `has_design_spec`
        # records that the value was assumed rather than supplied.
        nominal_vibration=payload.nominal_vibration if payload.nominal_vibration is not None else 2.0,
        nominal_temperature=payload.nominal_temperature if payload.nominal_temperature is not None else 65.0,
        nominal_rpm=payload.nominal_rpm if payload.nominal_rpm is not None else 1450.0,
        cost_center_code=payload.cost_center_code,
        ideal_units_per_hour=payload.ideal_units_per_hour,
        dataset_id=dataset_id,
        # A machine registered through the API is never simulator-owned, so the
        # live feed will not fabricate readings for it.
        data_origin=ORIGIN_SIMULATED if dataset_id is None else ORIGIN_EXTERNAL,
        has_design_spec=1 if has_spec else 0,
    )
    db.add(machine)
    await db.commit()
    await db.refresh(machine)

    await record_audit(
        db, actor, "MACHINE_CREATED", "machine", machine.id,
        f"{payload.name} (dataset {dataset_id})",
    )
    await db.commit()

    return MachineListItem(
        id=machine.id,
        name=machine.display_name or machine.name,
        type=machine.type,
        location=machine.location,
        criticality=machine.criticality,
        latest_risk=None,
        severity="Normal",
        maintenance_priority=0.0,
    )


@router.get("", response_model=List[MachineListItem])
async def list_machines(
    dataset_id: Optional[int] = Query(
        None,
        description="Restrict to one uploaded dataset. Omit for the built-in demo fleet.",
    ),
    db: AsyncSession = Depends(get_db),
):
    """List machines with latest risk score and severity.

    Defaults to the demo fleet (`dataset_id IS NULL`) so existing callers and the
    dashboard behave exactly as before.
    """
    query = select(Machine)
    if dataset_id is None:
        query = query.where(Machine.dataset_id.is_(None))
    else:
        query = query.where(Machine.dataset_id == dataset_id)
    result = await db.execute(query)
    machines = result.scalars().all()

    output = []
    for m in machines:
        # Get latest active alert
        alert_result = await db.execute(
            select(Alert)
            .where(Alert.machine_id == m.id)
            .where(Alert.status == "Active")
            .order_by(Alert.created_at.desc())
            .limit(1)
        )
        alert = alert_result.scalar_one_or_none()

        # Latest scored reading. Healthy machines have no alert, but they still
        # have a real risk score, and §42 ranks every machine by risk.
        risk_result = await db.execute(
            select(SensorReading.risk_score, SensorReading.downtime_minutes)
            .where(SensorReading.machine_id == m.id)
            .where(SensorReading.risk_score.isnot(None))
            .order_by(SensorReading.timestamp.desc())
            .limit(1)
        )
        scored_row = risk_result.first()
        latest_scored = scored_row[0] if scored_row else None
        latest_downtime = scored_row[1] if scored_row else 0.0

        if latest_scored is not None:
            latest_risk = latest_scored
        elif alert is not None:
            latest_risk = alert.risk_score
        else:
            latest_risk = None

        # Maintenance priority is a function of risk, criticality and production
        # impact (§27), so it can be computed for every machine — not only for
        # those that happen to have an open alert.
        if latest_risk is None:
            priority = None
        else:
            # Prefer the alert's stored impact: it was resolved from the ERP
            # schedule at scoring time, so reusing it keeps the list consistent
            # with the machine detail view instead of recomputing a weaker label.
            impact = (
                alert.production_impact if alert and alert.production_impact
                else get_production_impact(m.criticality, latest_downtime or 0.0)
            )
            priority = calculate_maintenance_priority(latest_risk, m.criticality, impact)

        output.append(MachineListItem(
            id=m.id,
            # Show the user's own label, never the internal dataset-scoped name.
            name=m.display_name or m.name,
            type=m.type,
            location=m.location,
            criticality=m.criticality,
            latest_risk=latest_risk,
            severity=classify_risk(latest_risk) if latest_risk is not None else "Normal",
            maintenance_priority=priority,
        ))

    # Sort by risk score descending
    output.sort(key=lambda x: x.latest_risk or 0, reverse=True)
    return output


@router.get("/{machine_id}", response_model=MachineDetailOut)
async def get_machine(machine_id: int, db: AsyncSession = Depends(get_db)):
    """Get detailed machine data: readings, maintenance, risk history, latest alert."""
    result = await db.execute(select(Machine).where(Machine.id == machine_id))
    machine = result.scalar_one_or_none()
    if not machine:
        raise HTTPException(status_code=404, detail="MACHINE_NOT_FOUND")

    # Last 48 sensor readings
    readings_result = await db.execute(
        select(SensorReading)
        .where(SensorReading.machine_id == machine_id)
        .order_by(SensorReading.timestamp.desc())
        .limit(48)
    )
    readings = readings_result.scalars().all()
    readings = list(reversed(readings))

    # Maintenance records
    maint_result = await db.execute(
        select(MaintenanceRecord)
        .where(MaintenanceRecord.machine_id == machine_id)
        .order_by(MaintenanceRecord.maintenance_date.desc())
        .limit(10)
    )
    maintenance = maint_result.scalars().all()

    # Risk history — from scored sensor readings, so the trend has real
    # resolution rather than one point per alert.
    risk_result = await db.execute(
        select(SensorReading.timestamp, SensorReading.risk_score)
        .where(SensorReading.machine_id == machine_id)
        .where(SensorReading.risk_score.isnot(None))
        .order_by(SensorReading.timestamp.desc())
        .limit(48)
    )
    risk_rows = list(reversed(risk_result.fetchall()))
    risk_history = [
        RiskHistoryPoint(timestamp=row[0], risk_score=row[1]) for row in risk_rows
    ]

    if not risk_history:
        # Fall back to alert history for machines seeded before per-reading
        # scoring existed, so the chart is never empty.
        alert_result = await db.execute(
            select(Alert)
            .where(Alert.machine_id == machine_id)
            .order_by(Alert.created_at.desc())
            .limit(30)
        )
        risk_history = [
            RiskHistoryPoint(timestamp=a.created_at, risk_score=a.risk_score)
            for a in reversed(alert_result.scalars().all())
        ]

    # Latest active alert
    latest_alert_result = await db.execute(
        select(Alert)
        .where(Alert.machine_id == machine_id)
        .where(Alert.status == "Active")
        .order_by(Alert.created_at.desc())
        .limit(1)
    )
    latest_alert = latest_alert_result.scalar_one_or_none()

    return MachineDetailOut(
        id=machine.id,
        name=machine.display_name or machine.name,
        type=machine.type,
        location=machine.location,
        install_date=machine.install_date,
        criticality=machine.criticality,
        nominal_vibration=machine.nominal_vibration,
        nominal_temperature=machine.nominal_temperature,
        nominal_rpm=machine.nominal_rpm,
        readings=[SensorReadingOut.model_validate(r) for r in readings],
        maintenance=[MaintenanceRecordOut.model_validate(m) for m in maintenance],
        risk_history=risk_history,
        latest_alert=AlertSummary.model_validate(latest_alert) if latest_alert else None,
    )
