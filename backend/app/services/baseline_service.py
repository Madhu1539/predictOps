"""
Baseline derivation — what "normal" looks like for a specific machine.

Two different ideas are deliberately kept apart:

  nominal_*   design specification. Authoritative when the operator supplied it.
  baseline_*  observed normal, derived from this machine's own readings.

The demo fleet has real design specs; an uploaded factory usually does not.
Rather than force one to stand in for the other, both are stored and
`reference_for()` applies a single documented precedence rule to every machine,
whatever its origin.

Derivation uses the machine's EARLIEST stable window, not its whole history. A
machine that degrades over 90 days would otherwise raise its own baseline and
partially mask the very degradation being detected.
"""
import logging
from typing import Dict, List, Optional, Sequence

import numpy as np

logger = logging.getLogger(__name__)

# Below this many readings a baseline is reported but flagged low confidence: a
# handful of points cannot establish what normal looks like.
MIN_TRUSTED_SAMPLES = 50

# Fraction of the earliest history treated as the candidate "settled" window.
EARLY_WINDOW_FRACTION = 0.30
# Never look at fewer than this many readings when the series is short.
MIN_WINDOW_READINGS = 24

# Robust central estimate. The median resists the upward pull of a degrading
# tail far better than the mean.
CENTRAL_PERCENTILE = 50


def _robust_centre(values: Sequence[float]) -> Optional[float]:
    clean = [float(v) for v in values if v is not None and np.isfinite(v)]
    if not clean:
        return None
    return float(np.percentile(clean, CENTRAL_PERCENTILE))


def derive_baselines(readings: List[dict]) -> Dict[str, object]:
    """Derive observed baselines from one machine's chronological readings.

    `readings` must be ordered oldest-first and contain vibration, temperature
    and rpm. Returns baseline values plus the provenance needed to report how
    they were obtained.
    """
    total = len(readings)
    if total == 0:
        return {
            "baseline_vibration": None,
            "baseline_temperature": None,
            "baseline_rpm": None,
            "baseline_method": "unavailable_no_readings",
            "baseline_sample_count": 0,
            "trusted": False,
        }

    window_size = max(MIN_WINDOW_READINGS, int(total * EARLY_WINDOW_FRACTION))
    window = readings[:window_size]
    used = len(window)

    baselines = {
        "baseline_vibration": _robust_centre([r.get("vibration") for r in window]),
        "baseline_temperature": _robust_centre([r.get("temperature") for r in window]),
        "baseline_rpm": _robust_centre([r.get("rpm") for r in window]),
    }

    trusted = used >= MIN_TRUSTED_SAMPLES
    method = (
        f"median_of_earliest_{used}_readings"
        if trusted
        else f"median_of_earliest_{used}_readings_low_confidence"
    )

    baselines["baseline_method"] = method
    baselines["baseline_sample_count"] = used
    baselines["trusted"] = trusted
    return baselines


def reference_for(machine) -> Dict[str, object]:
    """The reference values deviations should be measured against.

    Precedence, applied identically to demo and uploaded machines:

      1. `nominal_*` when a design spec was genuinely supplied
      2. otherwise the derived `baseline_*`
      3. otherwise the column defaults, clearly labelled as assumed

    Returning the source alongside the numbers means a report never has to guess
    what a percentage was relative to.
    """
    has_spec = bool(getattr(machine, "has_design_spec", 1))

    if has_spec:
        return {
            "vibration": machine.nominal_vibration,
            "temperature": machine.nominal_temperature,
            "rpm": machine.nominal_rpm,
            "source": "design_spec",
        }

    vibration = getattr(machine, "baseline_vibration", None)
    temperature = getattr(machine, "baseline_temperature", None)
    rpm = getattr(machine, "baseline_rpm", None)

    if vibration is not None and temperature is not None and rpm is not None:
        return {
            "vibration": vibration,
            "temperature": temperature,
            "rpm": rpm,
            "source": "derived_baseline",
        }

    # Nothing supplied and nothing derived yet. Fall back, but say so, because
    # deviations against an assumed reference are not trustworthy.
    return {
        "vibration": machine.nominal_vibration,
        "temperature": machine.nominal_temperature,
        "rpm": machine.nominal_rpm,
        "source": "assumed_default",
    }


def apply_baselines(machine, readings: List[dict]) -> Dict[str, object]:
    """Derive and write baselines onto a machine. Returns the derivation result.

    Only touches the `baseline_*` fields, never `nominal_*`, so a supplied design
    spec is never silently overwritten by observed data.
    """
    result = derive_baselines(readings)

    if result["baseline_vibration"] is not None:
        machine.baseline_vibration = result["baseline_vibration"]
    if result["baseline_temperature"] is not None:
        machine.baseline_temperature = result["baseline_temperature"]
    if result["baseline_rpm"] is not None:
        machine.baseline_rpm = result["baseline_rpm"]

    machine.baseline_method = result["baseline_method"]
    machine.baseline_sample_count = result["baseline_sample_count"]
    return result


# Cap on how much history is read for a derivation. The earliest window is what
# matters, so pulling the entire series would be wasted work on a long history.
BASELINE_READ_LIMIT = 500


async def refresh_machine_baseline(db, machine) -> Dict[str, object]:
    """Recompute one machine's observed baseline from its stored readings.

    Never raises: a missing baseline degrades the reference to `assumed_default`,
    which is reported honestly, whereas failing here would block ingestion.
    """
    try:
        from sqlalchemy import select

        from app.models.sensor import SensorReading

        rows = (
            await db.execute(
                select(SensorReading.vibration, SensorReading.temperature, SensorReading.rpm)
                .where(SensorReading.machine_id == machine.id)
                .order_by(SensorReading.timestamp.asc())
                .limit(BASELINE_READ_LIMIT)
            )
        ).all()

        readings = [
            {"vibration": v, "temperature": t, "rpm": r} for v, t, r in rows
        ]
        result = apply_baselines(machine, readings)
        await db.commit()
        return result
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(f"Baseline refresh failed for machine {machine.id}: {exc}")
        return {"baseline_method": "unavailable_error", "trusted": False}


async def backfill_all_baselines(db) -> int:
    """Populate observed baselines for every machine that lacks one.

    Applied to the demo fleet as well as uploads: the same fields are populated by
    the same code, so nothing about the built-in data is privileged.
    """
    from sqlalchemy import select

    from app.models.machine import Machine

    machines = (
        await db.execute(select(Machine).where(Machine.baseline_method.is_(None)))
    ).scalars().all()

    updated = 0
    for machine in machines:
        await refresh_machine_baseline(db, machine)
        updated += 1
    if updated:
        logger.info(f"Derived observed baselines for {updated} machine(s)")
    return updated
