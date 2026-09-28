"""
Cost and Value Model — converts risk and downtime into money.

Every figure this module produces is a MODELLED ESTIMATE built from ERP cost
data plus documented engineering assumptions. None of it is a measured saving.
Callers must label it as an estimate wherever it is displayed.

The model answers one question a plant manager actually asks: is it worth taking
this machine down now, rather than waiting for it to fail?

    expected value of acting = P(failure) x cost of unplanned failure
                               - cost of the planned intervention

A positive result means acting now is the cheaper bet.
"""
from typing import Dict, Optional

# Mean time to repair by failure mode, in hours. Unplanned failures take longer
# than planned work: the machine stops without warning, diagnosis happens under
# pressure, and parts may not be staged.
MTTR_UNPLANNED_HOURS: Dict[str, float] = {
    "BEARING_DEGRADATION": 8.0,
    "OVERHEATING": 6.0,
    "MOTOR_DEGRADATION": 10.0,
    "MISALIGNMENT": 5.0,
    "GENERAL_MECHANICAL_WEAR": 6.0,
}

# Planned intervention on the same failure mode, done during a scheduled window.
MTTR_PLANNED_HOURS: Dict[str, float] = {
    "BEARING_DEGRADATION": 3.0,
    "OVERHEATING": 2.0,
    "MOTOR_DEGRADATION": 4.0,
    "MISALIGNMENT": 2.0,
    "GENERAL_MECHANICAL_WEAR": 2.5,
}

DEFAULT_MTTR_UNPLANNED_HOURS = 6.0
DEFAULT_MTTR_PLANNED_HOURS = 2.5

# Maintenance labour, fully loaded, per hour.
LABOUR_RATE_PER_HOUR = 65.0

# Technicians required for an unplanned breakdown vs planned work.
UNPLANNED_CREW_SIZE = 2
PLANNED_CREW_SIZE = 1

# Fallback when a machine has no cost centre. Deliberately non-zero so a missing
# ERP link degrades to a conservative estimate rather than silently reporting
# that downtime is free.
DEFAULT_DOWNTIME_COST_PER_HOUR = 1200.0

# A planned stop still loses some production, but far less than a breakdown:
# it is scheduled into a window rather than interrupting a running order.
PLANNED_PRODUCTION_LOSS_FACTOR = 0.35


def get_downtime_cost_per_hour(cost_center: Optional[object]) -> float:
    """Hourly cost of this machine being down, from the ERP cost centre."""
    if cost_center is None:
        return DEFAULT_DOWNTIME_COST_PER_HOUR
    rate = getattr(cost_center, "downtime_cost_per_hour", None)
    if not rate or rate <= 0:
        return DEFAULT_DOWNTIME_COST_PER_HOUR
    return float(rate)


def get_currency(cost_center: Optional[object]) -> str:
    if cost_center is None:
        return "USD"
    return getattr(cost_center, "currency", None) or "USD"


def estimate_unplanned_failure_cost(
    failure_mode: Optional[str],
    cost_center: Optional[object] = None,
) -> float:
    """Cost if this machine runs to failure: lost production plus repair labour."""
    hours = MTTR_UNPLANNED_HOURS.get(failure_mode or "", DEFAULT_MTTR_UNPLANNED_HOURS)
    downtime_cost = hours * get_downtime_cost_per_hour(cost_center)
    labour_cost = hours * LABOUR_RATE_PER_HOUR * UNPLANNED_CREW_SIZE
    return round(downtime_cost + labour_cost, 2)


def estimate_planned_intervention_cost(
    failure_mode: Optional[str],
    cost_center: Optional[object] = None,
    material: Optional[object] = None,
) -> float:
    """Cost of acting now: part, labour, and the shorter scheduled stop."""
    hours = MTTR_PLANNED_HOURS.get(failure_mode or "", DEFAULT_MTTR_PLANNED_HOURS)

    part_cost = 0.0
    if material is not None:
        part_cost = float(getattr(material, "unit_cost", 0.0) or 0.0)

    labour_cost = hours * LABOUR_RATE_PER_HOUR * PLANNED_CREW_SIZE
    production_loss = (
        hours * get_downtime_cost_per_hour(cost_center) * PLANNED_PRODUCTION_LOSS_FACTOR
    )
    return round(part_cost + labour_cost + production_loss, 2)


def estimate_loss_avoided(
    risk_score: float,
    failure_mode: Optional[str] = None,
    cost_center: Optional[object] = None,
    material: Optional[object] = None,
) -> float:
    """Expected value of acting now instead of running to failure.

    (P(failure) x unplanned cost) - planned intervention cost.

    Clamped at zero: when the probability is low the rational choice is to wait,
    and reporting a negative "saving" would be noise rather than insight.
    """
    probability = max(0.0, min(float(risk_score or 0.0), 100.0)) / 100.0
    expected_unplanned = probability * estimate_unplanned_failure_cost(
        failure_mode, cost_center
    )
    planned = estimate_planned_intervention_cost(failure_mode, cost_center, material)
    return round(max(expected_unplanned - planned, 0.0), 2)


def estimate_downtime_cost(downtime_minutes: float, cost_center: Optional[object] = None) -> float:
    """Cost of downtime already incurred."""
    hours = max(float(downtime_minutes or 0.0), 0.0) / 60.0
    return round(hours * get_downtime_cost_per_hour(cost_center), 2)


def estimate_oee_gap_cost(
    oee: float,
    target_oee: float,
    hours: float,
    cost_center: Optional[object] = None,
) -> float:
    """Cost of the gap between current OEE and a target, over a period.

    Used to express "where should I improve" in money rather than percentage
    points.
    """
    gap = max(float(target_oee) - float(oee), 0.0)
    return round(gap * max(hours, 0.0) * get_downtime_cost_per_hour(cost_center), 2)


ASSUMPTIONS = {
    "labour_rate_per_hour": LABOUR_RATE_PER_HOUR,
    "unplanned_crew_size": UNPLANNED_CREW_SIZE,
    "planned_crew_size": PLANNED_CREW_SIZE,
    "mttr_unplanned_hours": MTTR_UNPLANNED_HOURS,
    "mttr_planned_hours": MTTR_PLANNED_HOURS,
    "planned_production_loss_factor": PLANNED_PRODUCTION_LOSS_FACTOR,
    "default_downtime_cost_per_hour": DEFAULT_DOWNTIME_COST_PER_HOUR,
    "basis": (
        "Modelled estimate. Downtime cost per hour comes from the ERP cost centre; "
        "repair durations, crew sizes and labour rate are engineering assumptions. "
        "These are not measured savings."
    ),
}
