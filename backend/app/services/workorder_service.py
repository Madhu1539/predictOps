"""
Work Order Service — creates work orders from alerts, assigns technicians,
manages status transitions (spec §34–36).
"""
import logging
from datetime import datetime, timedelta
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.config import get_settings
from app.models.work_order import WorkOrder
from app.models.alert import Alert
from app.models.technician import Technician
from app.models.oee import OeeSnapshot

logger = logging.getLogger(__name__)

# A work order in one of these states still needs someone to act on it, so a
# second one for the same machine would be duplicate work.
OPEN_WORK_ORDER_STATUSES = ("Open", "InProgress")

# Simulated availability recovered when maintenance is completed (spec §38).
# Presented as a simulation, never as measured real-world savings.
RECOVERED_AVAILABILITY_GAIN = 0.08


def get_due_date(severity: str) -> datetime:
    """
    Work order due date based on severity (spec §36):
    Critical → next business day (1 working day)
    Warning  → within 7 days
    Watch    → within 14 days (planned maintenance)
    """
    now = datetime.utcnow()
    if severity == "Critical":
        return now + timedelta(days=1)
    elif severity == "Warning":
        return now + timedelta(days=7)
    else:
        return now + timedelta(days=14)


def get_work_order_priority(severity: str) -> str:
    """Map alert severity to work order priority (spec §36)."""
    mapping = {
        "Critical": "Urgent",
        "Warning": "Medium",
        "Watch": "Low",
    }
    return mapping.get(severity, "Low")


async def get_least_busy_technician(db: AsyncSession) -> Optional[str]:
    """Assign work order to technician with lowest current workload (spec §35)."""
    result = await db.execute(
        select(Technician).order_by(Technician.active_work_orders)
    )
    technicians = result.scalars().all()
    if technicians:
        return technicians[0].name
    return "Unassigned"


async def create_work_order_from_alert(
    db: AsyncSession,
    alert: Alert,
    machine_name: str,
) -> WorkOrder:
    """Auto-populate and store a work order from an alert."""
    technician = await get_least_busy_technician(db)

    wo = WorkOrder(
        alert_id=alert.id,
        machine_id=alert.machine_id,
        technician=technician,
        part_needed=alert.part_needed,
        priority=get_work_order_priority(alert.severity),
        due_date=get_due_date(alert.severity),
        status="Open",
        created_at=datetime.utcnow(),
    )
    db.add(wo)

    # Increment technician workload
    if technician != "Unassigned":
        result = await db.execute(
            select(Technician).where(Technician.name == technician)
        )
        tech = result.scalar_one_or_none()
        if tech:
            tech.active_work_orders += 1

    await db.commit()
    await db.refresh(wo)
    return wo


async def has_open_work_order(db: AsyncSession, machine_id: int) -> bool:
    """Whether a machine already has a work order awaiting action.

    The duplicate guard for automatic creation. The live feed re-scores every
    machine on each tick, so without this a machine that stays Critical would
    accumulate a work order per tick — four per minute at the default interval.
    """
    result = await db.execute(
        select(WorkOrder.id)
        .where(WorkOrder.machine_id == machine_id)
        .where(WorkOrder.status.in_(OPEN_WORK_ORDER_STATUSES))
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


async def auto_create_work_order(
    db: AsyncSession,
    alert: Alert,
    machine_name: str,
) -> Optional[WorkOrder]:
    """Raise a work order for a qualifying alert without waiting for a planner.

    Returns the work order, or None when automation is disabled, the severity does
    not qualify, or the machine already has one open.

    Fail-soft by design: a failure here is logged and swallowed, because losing an
    automatic work order must not stop the alert that prompted it from being saved.
    """
    settings = get_settings()
    if not settings.auto_work_order_enabled:
        return None
    if alert.severity not in settings.auto_work_order_severity_set:
        return None

    try:
        if await has_open_work_order(db, alert.machine_id):
            logger.debug(
                "Machine %s already has an open work order; not raising another.",
                machine_name,
            )
            return None

        wo = await create_work_order_from_alert(db, alert, machine_name)
        logger.info(
            "Auto-raised work order %s for %s (%s, priority %s, due %s)",
            wo.id, machine_name, alert.severity, wo.priority,
            wo.due_date.date() if wo.due_date else "n/a",
        )
        return wo
    except Exception as exc:
        logger.warning(
            "Automatic work order for %s failed: %s", machine_name, str(exc)[:200]
        )
        return None


async def complete_work_order(db: AsyncSession, work_order: WorkOrder) -> WorkOrder:
    """Complete a work order, release the technician, resolve the originating
    alert, and apply the simulated OEE recovery (spec §38).

    The OEE effect is a SIMULATION: completing maintenance removes the modelled
    downtime penalty from the machine's next snapshot. It is not a measurement
    of real prevented downtime.
    """
    work_order.status = "Completed"
    work_order.completed_at = datetime.utcnow()

    if work_order.technician and work_order.technician != "Unassigned":
        result = await db.execute(
            select(Technician).where(Technician.name == work_order.technician)
        )
        tech = result.scalar_one_or_none()
        if tech and tech.active_work_orders > 0:
            tech.active_work_orders -= 1

    # Resolve the alert that triggered this work order.
    if work_order.alert_id:
        alert_result = await db.execute(
            select(Alert).where(Alert.id == work_order.alert_id)
        )
        alert = alert_result.scalar_one_or_none()
        if alert and alert.status in ("Active", "Acknowledged"):
            alert.status = "Resolved"

    await _apply_simulated_oee_recovery(db, work_order.machine_id)

    await db.commit()
    await db.refresh(work_order)
    return work_order


async def _apply_simulated_oee_recovery(db: AsyncSession, machine_id: int) -> None:
    """Write a post-maintenance OEE snapshot with the downtime penalty removed.

    Availability is recomputed against full planned production time, which is
    what a machine restored to health would achieve. Performance and quality
    carry over from the latest snapshot so the improvement is attributable
    solely to recovered availability.
    """
    result = await db.execute(
        select(OeeSnapshot)
        .where(OeeSnapshot.machine_id == machine_id)
        .order_by(OeeSnapshot.timestamp.desc())
        .limit(1)
    )
    latest = result.scalar_one_or_none()
    if latest is None:
        return

    # Recover availability toward nominal, capped at 1.0.
    recovered_availability = min(round(latest.availability + RECOVERED_AVAILABILITY_GAIN, 4), 1.0)
    if recovered_availability <= latest.availability:
        return

    oee = round(recovered_availability * latest.performance * latest.quality, 4)
    db.add(
        OeeSnapshot(
            machine_id=machine_id,
            timestamp=datetime.utcnow(),
            availability=recovered_availability,
            performance=latest.performance,
            quality=latest.quality,
            oee=oee,
        )
    )
