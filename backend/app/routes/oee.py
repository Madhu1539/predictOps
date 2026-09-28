"""
OEE API — GET /api/oee
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from typing import List

from app.database import get_db
from app.models.oee import OeeSnapshot
from app.models.machine import Machine
from app.models.cost_center import CostCenter
from app.schemas import (
    OeeDashboardOut, OeeTrendPoint, MachineOeeSummary,
    OeeLossesOut, OeeLossItem,
)
from app.services.oee_service import aggregate_plant_oee
from app.services import cost_service

router = APIRouter(prefix="/api/oee", tags=["oee"])

# World-class OEE benchmark, used as the improvement target for gap costing.
TARGET_OEE = 0.85


@router.get("/losses", response_model=OeeLossesOut)
async def get_oee_losses(
    target_oee: float = Query(TARGET_OEE, ge=0.1, le=1.0),
    window_hours: float = Query(24.0, gt=0, le=8760),
    db: AsyncSession = Depends(get_db),
):
    """Rank machines by OEE loss and price the gap to target.

    OEE alone tells an operator that something is wrong but not what to fix
    first. Decomposing into availability / performance / quality loss and costing
    the gap turns the metric into a work queue.

    Registered before `/{...}`-style routes are added so a literal path segment
    can never be captured as a parameter.
    """
    cost_centers = {
        cc.code: cc for cc in (await db.execute(select(CostCenter))).scalars().all()
    }
    machines = (await db.execute(select(Machine))).scalars().all()

    items = []
    loss_totals = {"availability": 0.0, "performance": 0.0, "quality": 0.0}
    total_gap_cost = 0.0

    for machine in machines:
        snapshot = (
            await db.execute(
                select(OeeSnapshot)
                .where(OeeSnapshot.machine_id == machine.id)
                .order_by(OeeSnapshot.timestamp.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if snapshot is None:
            continue

        # Loss attribution: each factor's shortfall from 1.0, scaled by the
        # factors upstream of it, so the three losses plus the achieved OEE sum
        # to 1.0 rather than triple-counting the same lost output.
        availability_loss = 1.0 - snapshot.availability
        performance_loss = snapshot.availability * (1.0 - snapshot.performance)
        quality_loss = snapshot.availability * snapshot.performance * (1.0 - snapshot.quality)

        losses = {
            "availability": availability_loss,
            "performance": performance_loss,
            "quality": quality_loss,
        }
        biggest = max(losses, key=losses.get)

        cc = cost_centers.get(machine.cost_center_code)
        gap_cost = cost_service.estimate_oee_gap_cost(
            snapshot.oee, target_oee, window_hours, cc
        )

        for key, value in losses.items():
            loss_totals[key] += value
        total_gap_cost += gap_cost

        items.append(
            OeeLossItem(
                machine_id=machine.id,
                machine_name=machine.name,
                cost_center_code=machine.cost_center_code,
                oee=round(snapshot.oee, 4),
                availability=round(snapshot.availability, 4),
                performance=round(snapshot.performance, 4),
                quality=round(snapshot.quality, 4),
                availability_loss=round(availability_loss, 4),
                performance_loss=round(performance_loss, 4),
                quality_loss=round(quality_loss, 4),
                biggest_loss=biggest,
                biggest_loss_pct=round(losses[biggest] * 100, 2),
                estimated_gap_cost=gap_cost,
            )
        )

    # Worst OEE first: this is the Pareto ordering the operator should work down.
    items.sort(key=lambda i: i.oee)

    plant_oee = (
        round(sum(i.oee for i in items) / len(items), 4) if items else 0.0
    )
    count = len(items) or 1

    return OeeLossesOut(
        plant_oee=plant_oee,
        target_oee=round(target_oee, 4),
        window_hours=window_hours,
        machines=items,
        loss_totals={k: round(v / count, 4) for k, v in loss_totals.items()},
        total_gap_cost=round(total_gap_cost, 2),
        basis=cost_service.ASSUMPTIONS["basis"],
    )


@router.get("", response_model=OeeDashboardOut)
async def get_oee(db: AsyncSession = Depends(get_db)):
    """Returns plant-level OEE, trend, and per-machine breakdown.

    All values are 0-1 as specified in §37 and the §46 contract. Converting to
    percentages is the presentation layer's job.
    """

    # Global OEE trend (last 30 snapshots aggregated by timestamp bucket)
    trend_result = await db.execute(
        select(OeeSnapshot).order_by(OeeSnapshot.timestamp.desc()).limit(200)
    )
    all_snapshots = trend_result.scalars().all()

    # Per-machine latest OEE
    result = await db.execute(select(Machine))
    machines = result.scalars().all()

    per_machine = []
    machine_oees = []

    for machine in machines:
        snap_result = await db.execute(
            select(OeeSnapshot)
            .where(OeeSnapshot.machine_id == machine.id)
            .order_by(OeeSnapshot.timestamp.desc())
            .limit(1)
        )
        snap = snap_result.scalar_one_or_none()
        if snap:
            machine_oees.append({"oee": snap.oee})
            per_machine.append(MachineOeeSummary(
                machine_id=machine.id,
                machine_name=machine.name,
                oee=round(snap.oee, 4),
                availability=round(snap.availability, 4),
                performance=round(snap.performance, 4),
                quality=round(snap.quality, 4),
            ))

    plant_oee = aggregate_plant_oee(machine_oees)

    # Build trend from snapshots — aggregate per time bucket
    import pandas as pd
    from datetime import datetime

    trend = []
    if all_snapshots:
        snap_df = pd.DataFrame([{
            "timestamp": s.timestamp,
            "oee": s.oee,
            "availability": s.availability,
            "performance": s.performance,
            "quality": s.quality,
        } for s in all_snapshots])
        snap_df = snap_df.sort_values("timestamp")
        # Resample into hourly buckets
        snap_df.set_index("timestamp", inplace=True)
        snap_df.index = pd.to_datetime(snap_df.index)
        resampled = snap_df.resample("1h").mean().dropna().tail(30)
        for ts, row in resampled.iterrows():
            trend.append(OeeTrendPoint(
                timestamp=ts,
                oee=round(row["oee"], 4),
                availability=round(row["availability"], 4),
                performance=round(row["performance"], 4),
                quality=round(row["quality"], 4),
            ))

    return OeeDashboardOut(
        plant_oee=round(plant_oee, 4),
        trend=trend,
        per_machine=per_machine,
    )
