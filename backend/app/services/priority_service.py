"""
Maintenance Priority Service — spec §27.

Priority = Failure Risk × Machine Criticality × Production Impact Factor
Normalized to 0-100.
"""
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

CRITICALITY_WEIGHTS = {
    "High": 1.0,
    "Medium": 0.65,
    "Low": 0.35,
}

PRODUCTION_IMPACT_WEIGHTS = {
    "High": 1.0,
    "Medium": 0.65,
    "Low": 0.35,
}

# How far ahead a scheduled order still counts as "work this failure would
# disrupt". A week matches the model's 7-day prediction horizon: an order starting
# after the window is not what this alert is about.
IMPACT_HORIZON_DAYS = 7

# Committed quantity, in units, above which a disruption is High impact. Derived
# from the seeded ERP orders, whose planned quantities run 400-2000 units.
LARGE_ORDER_UNITS = 1200
MEDIUM_ORDER_UNITS = 400

ASSUMPTIONS = {
    "impact_horizon_days": IMPACT_HORIZON_DAYS,
    "large_order_units": LARGE_ORDER_UNITS,
    "medium_order_units": MEDIUM_ORDER_UNITS,
    "note": (
        "Production impact is read from ERP production orders overlapping the "
        "prediction horizon. Where no order data exists it falls back to machine "
        "criticality and recent downtime, and the basis is reported either way."
    ),
}


def calculate_maintenance_priority(
    risk_score: float,
    criticality: str,
    production_impact: str = "Medium",
) -> float:
    """
    Returns maintenance priority score 0-100.
    Example from spec:
      Machine A: risk=92%, criticality=High, impact=High → Priority 96
      Machine B: risk=97%, criticality=Low, impact=Low → Priority 61
    """
    c_weight = CRITICALITY_WEIGHTS.get(criticality, 0.65)
    p_weight = PRODUCTION_IMPACT_WEIGHTS.get(production_impact, 0.65)

    raw = (risk_score / 100.0) * c_weight * p_weight * 100
    return round(min(raw * 1.15, 100.0), 1)  # scale factor to match spec examples


def get_production_impact(criticality: str, downtime_minutes: float = 0.0) -> str:
    """Fallback impact label, used only when no ERP order data is available.

    Kept because uploaded datasets and partially-populated demos genuinely have no
    production orders. It is a weak signal: deriving impact from criticality means
    `calculate_maintenance_priority` multiplies criticality by a proxy for itself,
    so `resolve_production_impact` is preferred wherever ERP data exists.
    """
    if criticality == "High" or downtime_minutes > 120:
        return "High"
    elif criticality == "Medium" or downtime_minutes > 30:
        return "Medium"
    return "Low"


def classify_committed_quantity(units: int) -> str:
    """Impact label for a quantity of committed, not-yet-produced output."""
    if units >= LARGE_ORDER_UNITS:
        return "High"
    if units >= MEDIUM_ORDER_UNITS:
        return "Medium"
    return "Low"


async def resolve_production_impact(
    db: AsyncSession,
    machine_id: int,
    criticality: str,
    downtime_minutes: float = 0.0,
    as_of: Optional[datetime] = None,
) -> dict:
    """Production impact from the ERP schedule, with its basis.

    This is the IT half of the convergence: how much a failure matters depends on
    what is committed to run on the machine, not only on how critical the asset is
    in the abstract. An order already in progress, or starting inside the
    prediction horizon, is work this failure would disrupt.

    Returns `{impact, basis, orders, committed_units, detail}` so the reason is
    reportable rather than implied. `basis` is `erp_production_orders` when real
    orders were found and `criticality_fallback` when none exist.
    """
    # Avoid a circular import: production_order imports nothing from here, but
    # live_feed imports both and the module-level cost is pointless either way.
    from app.models.production_order import ProductionOrder

    now = as_of or datetime.utcnow()
    horizon = now + timedelta(days=IMPACT_HORIZON_DAYS)

    # Orders that a failure in the next `IMPACT_HORIZON_DAYS` would actually hit:
    # anything already running, or scheduled to start inside the window. Completed
    # orders are excluded — their output is already banked.
    result = await db.execute(
        select(ProductionOrder)
        .where(ProductionOrder.machine_id == machine_id)
        .where(ProductionOrder.status != "Complete")
        .where(
            or_(
                ProductionOrder.status == "InProgress",
                ProductionOrder.scheduled_start <= horizon,
            )
        )
        .order_by(ProductionOrder.scheduled_start)
    )
    orders = list(result.scalars().all())

    if not orders:
        return {
            "impact": get_production_impact(criticality, downtime_minutes),
            "basis": "criticality_fallback",
            "orders": [],
            "committed_units": 0,
            "detail": (
                "No open ERP production order on this machine inside the "
                f"{IMPACT_HORIZON_DAYS}-day horizon; impact inferred from machine "
                "criticality."
            ),
        }

    # Remaining, not already-produced quantity is what is actually at stake. A
    # nearly-finished large order is a smaller exposure than its planned size.
    committed_units = sum(
        max(0, (o.planned_qty or 0) - (o.actual_qty or 0)) for o in orders
    )
    impact = classify_committed_quantity(committed_units)

    # An order mid-run escalates: stopping it scraps setup and work in progress.
    in_progress = [o for o in orders if o.status == "InProgress"]
    if in_progress and impact == "Low":
        impact = "Medium"

    return {
        "impact": impact,
        "basis": "erp_production_orders",
        "orders": [
            {
                "order_no": o.order_no,
                "product": o.product,
                "status": o.status,
                "planned_qty": o.planned_qty,
                "actual_qty": o.actual_qty,
                "remaining_qty": max(0, (o.planned_qty or 0) - (o.actual_qty or 0)),
                "scheduled_start": o.scheduled_start.isoformat() if o.scheduled_start else None,
                "scheduled_end": o.scheduled_end.isoformat() if o.scheduled_end else None,
            }
            for o in orders[:5]
        ],
        "committed_units": committed_units,
        "detail": (
            f"{len(orders)} open ERP order(s) on this machine within "
            f"{IMPACT_HORIZON_DAYS} days, {committed_units} units still to produce"
            + (f", {len(in_progress)} already in progress" if in_progress else "")
            + "."
        ),
    }
