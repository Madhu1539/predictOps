"""
OEE Service — calculates OEE = Availability × Performance × Quality (spec §37).
"""
from typing import List, Dict, Optional


# Plant default design rate in UNITS PER HOUR, used when a machine has no
# ERP-supplied `ideal_units_per_hour`. The synthetic line runs at roughly 8-12
# units per minute, so the design maximum is 12/min = 720/hour.
DEFAULT_IDEAL_UNITS_PER_HOUR = 720.0

# Equivalent cycle time in MINUTES PER UNIT. Kept for backward compatibility.
# Using 1.0 min/unit would make (ideal_cycle_time x count) exceed run time on
# every reading, pinning Performance at its 1.0 cap and reducing OEE to Quality
# alone.
IDEAL_CYCLE_TIME = 60.0 / DEFAULT_IDEAL_UNITS_PER_HOUR


def calculate_oee(
    planned_production_time: float,
    actual_run_time: float,
    production_count: int,
    good_count: int,
    downtime_minutes: float = 0.0,
    ideal_units_per_hour: Optional[float] = None,
) -> Dict[str, float]:
    """
    Returns dict with: availability, performance, quality, oee (all 0-1).

    OEE = Availability × Performance × Quality
    Availability = actual_run_time / planned_production_time
    Performance = (ideal_cycle_time × total_count) / actual_run_time
    Quality = good_count / total_count

    `ideal_units_per_hour` comes from the machine's ERP-supplied design rate when
    available; a single plant-wide constant would understate Performance on slow
    machines and overstate it on fast ones. `downtime_minutes` is accepted for
    call-site symmetry but is not needed: it is already reflected in the gap
    between planned and actual run time.
    """
    if planned_production_time <= 0:
        return {"availability": 0.0, "performance": 0.0, "quality": 0.0, "oee": 0.0}

    rate = ideal_units_per_hour or DEFAULT_IDEAL_UNITS_PER_HOUR
    ideal_cycle_time = 60.0 / rate if rate > 0 else IDEAL_CYCLE_TIME

    availability = min(actual_run_time / planned_production_time, 1.0)

    if actual_run_time > 0 and production_count > 0:
        performance = min((ideal_cycle_time * production_count) / actual_run_time, 1.0)
    else:
        performance = 0.0

    if production_count > 0:
        quality = min(good_count / production_count, 1.0)
    else:
        quality = 1.0  # no production = no defects

    oee = availability * performance * quality

    return {
        "availability": round(availability, 4),
        "performance": round(performance, 4),
        "quality": round(quality, 4),
        "oee": round(oee, 4),
    }


def aggregate_plant_oee(machine_oees: List[Dict]) -> float:
    """Average OEE across all machines."""
    if not machine_oees:
        return 0.0
    return round(sum(m["oee"] for m in machine_oees) / len(machine_oees), 4)
