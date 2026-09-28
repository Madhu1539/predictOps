"""
Data quality validation.

Runs against any dataset, the built-in demo fleet included. That is deliberate: a
quality check that only ever runs on uploads is unverifiable theatre, whereas one
that also passes on the demo data can be demonstrated to work.

Scoring data without checking it first is how a system produces confident nonsense.
A flat-lined sensor, for example, looks like a perfectly stable machine right up
until someone notices the transducer is dead.
"""
import logging
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# The rolling window `engineer_features` uses. Below this, rolling means, standard
# deviations and slopes are computed on a partial window and are weaker than
# anything the model saw in training.
MIN_READINGS_FOR_FULL_FEATURES = 24

# A gap larger than this multiple of the inferred cadence counts as missing data
# rather than ordinary jitter.
GAP_TOLERANCE_MULTIPLE = 3.0

# Physically implausible bounds. Outside these the sensor or the unit is wrong,
# not the machine.
PLAUSIBLE_BOUNDS = {
    "vibration": (0.0, 100.0),        # mm/s RMS
    "temperature": (-50.0, 400.0),    # C
    "rpm": (0.0, 30000.0),
}

SENSORS = ("vibration", "temperature", "rpm")


def _machine_checks(frame: pd.DataFrame, label: str) -> Dict[str, object]:
    """Per-machine checks. `frame` must be chronological for one machine."""
    issues: List[Dict[str, str]] = []
    count = len(frame)

    # ── Cadence and gaps ──────────────────────────────────────────────────────
    cadence_minutes: Optional[float] = None
    gaps: List[Dict[str, object]] = []
    duplicate_timestamps = 0

    if count >= 2 and "timestamp" in frame:
        stamps = pd.to_datetime(frame["timestamp"]).sort_values()
        duplicate_timestamps = int(stamps.duplicated().sum())
        deltas = stamps.diff().dropna().dt.total_seconds() / 60.0
        positive = deltas[deltas > 0]
        if not positive.empty:
            cadence_minutes = float(positive.median())
            threshold = cadence_minutes * GAP_TOLERANCE_MULTIPLE
            for position, delta in positive.items():
                if delta > threshold:
                    gaps.append({
                        "after": str(stamps.loc[:position].iloc[-2]),
                        "gap_minutes": round(float(delta), 1),
                    })
            if gaps:
                issues.append({
                    "check": "gaps",
                    "severity": "warning",
                    "detail": (
                        f"{len(gaps)} gap(s) longer than "
                        f"{GAP_TOLERANCE_MULTIPLE:g}x the {cadence_minutes:.0f} min cadence"
                    ),
                })

    if duplicate_timestamps:
        issues.append({
            "check": "duplicate_timestamps",
            "severity": "warning",
            "detail": f"{duplicate_timestamps} duplicate timestamp(s)",
        })

    # ── Per-sensor checks ─────────────────────────────────────────────────────
    sensor_stats: Dict[str, Dict[str, object]] = {}
    present_sensors: List[str] = []
    absent_sensors: List[str] = []

    for sensor in SENSORS:
        series = pd.to_numeric(frame[sensor], errors="coerce") if sensor in frame else None
        if series is None or series.dropna().empty:
            # Not instrumented. This is normal on real equipment and is reported as
            # a fact about coverage, not as a data defect.
            absent_sensors.append(sensor)
            sensor_stats[sensor] = {"instrumented": False}
            continue

        present_sensors.append(sensor)
        missing = int(series.isna().sum())
        clean = series.dropna()

        stats: Dict[str, object] = {
            "instrumented": True,
            "missing": missing,
            "min": round(float(clean.min()), 3),
            "max": round(float(clean.max()), 3),
            "mean": round(float(clean.mean()), 3),
            "std": round(float(clean.std()), 4) if len(clean) > 1 else 0.0,
        }

        if missing:
            issues.append({
                "check": "missing_values",
                "severity": "warning",
                "detail": f"{sensor}: {missing}/{count} readings missing a value",
            })

        # A dead transducer reads perfectly steady, which otherwise looks like an
        # exceptionally healthy machine.
        #
        # RPM is treated as a warning rather than an error: a VFD-controlled motor
        # held at a fixed setpoint, or telemetry rounded to whole revolutions, can
        # legitimately report a constant value. Vibration and temperature always
        # fluctuate on a running machine, so zero variance there really is a fault.
        if len(clean) >= 10 and float(clean.std()) == 0.0:
            stats["flatlined"] = True
            issues.append({
                "check": "flatlined_sensor",
                "severity": "warning" if sensor == "rpm" else "error",
                "detail": (
                    f"{sensor}: zero variance across {len(clean)} readings"
                    + (
                        " (may be a fixed-setpoint drive, or a dead sensor)"
                        if sensor == "rpm"
                        else ", likely a dead sensor"
                    )
                ),
            })

        low, high = PLAUSIBLE_BOUNDS[sensor]
        implausible = int(((clean < low) | (clean > high)).sum())
        if implausible:
            stats["implausible"] = implausible
            issues.append({
                "check": "implausible_values",
                "severity": "error",
                "detail": f"{sensor}: {implausible} value(s) outside {low}-{high}",
            })

        sensor_stats[sensor] = stats

    # ── History sufficiency ───────────────────────────────────────────────────
    if count < MIN_READINGS_FOR_FULL_FEATURES:
        issues.append({
            "check": "insufficient_history",
            "severity": "warning",
            "detail": (
                f"{count} readings; {MIN_READINGS_FOR_FULL_FEATURES} are needed for a "
                "full rolling window, so trend features are weaker than in training"
            ),
        })

    # ── Sensor coverage ───────────────────────────────────────────────────────
    if not present_sensors:
        issues.append({
            "check": "no_sensors",
            "severity": "error",
            "detail": "no vibration, temperature or rpm values at all",
        })
    elif absent_sensors:
        issues.append({
            "check": "partial_instrumentation",
            "severity": "warning",
            "detail": (
                f"no {', '.join(absent_sensors)} channel. Deviation and absolute "
                "limits still apply; the ML risk score needs all three and will be "
                "reported as unavailable"
            ),
        })

    return {
        "machine": label,
        "readings": count,
        "cadence_minutes": round(cadence_minutes, 1) if cadence_minutes else None,
        "first_reading": str(frame["timestamp"].min()) if "timestamp" in frame and count else None,
        "last_reading": str(frame["timestamp"].max()) if "timestamp" in frame and count else None,
        "sensors": sensor_stats,
        "present_sensors": present_sensors,
        "absent_sensors": absent_sensors,
        "gaps": gaps[:10],
        "issues": issues,
        "usable": not any(i["severity"] == "error" for i in issues),
    }


def assess_quality(readings: List[dict]) -> Dict[str, object]:
    """Validate a set of readings before they are trusted for scoring.

    `readings` carries machine (label), timestamp and the three sensors. Returns a
    per-machine breakdown plus an overall verdict:

        ok        nothing of concern
        warnings  usable, but something reduces confidence
        unusable  at least one machine has a disqualifying problem
    """
    if not readings:
        return {
            "overall": "unusable",
            "summary": {"machines": 0, "readings": 0},
            "machines": [],
            "issues": [{"check": "empty", "severity": "error", "detail": "no readings supplied"}],
        }

    frame = pd.DataFrame(readings)
    if "machine" not in frame:
        frame["machine"] = "unknown"

    per_machine = []
    for label, group in frame.groupby("machine", sort=True):
        ordered = group.sort_values("timestamp") if "timestamp" in group else group
        per_machine.append(_machine_checks(ordered.reset_index(drop=True), str(label)))

    all_issues = [
        # Attribute each issue to its machine. Aggregated across a fleet, otherwise
        # identical details appear repeatedly with no way to tell them apart.
        {**issue, "machine": m["machine"], "detail": f"{m['machine']}: {issue['detail']}"}
        for m in per_machine
        for issue in m["issues"]
    ]
    errors = [i for i in all_issues if i["severity"] == "error"]
    warnings = [i for i in all_issues if i["severity"] == "warning"]

    if errors:
        overall = "unusable"
    elif warnings:
        overall = "warnings"
    else:
        overall = "ok"

    span_start = span_end = None
    if "timestamp" in frame:
        stamps = pd.to_datetime(frame["timestamp"], errors="coerce").dropna()
        if not stamps.empty:
            span_start, span_end = str(stamps.min()), str(stamps.max())

    return {
        "overall": overall,
        "summary": {
            "machines": len(per_machine),
            "readings": len(frame),
            "first_reading": span_start,
            "last_reading": span_end,
            "error_count": len(errors),
            "warning_count": len(warnings),
            "unusable_machines": [m["machine"] for m in per_machine if not m["usable"]],
        },
        "machines": per_machine,
        "issues": errors + warnings,
        "guidance": _guidance(overall, errors, warnings),
    }


def _guidance(overall: str, errors: List[dict], warnings: List[dict]) -> str:
    if overall == "ok":
        return "No data quality concerns. Scores can be read at face value."
    if overall == "warnings":
        return (
            "Usable, but confidence is reduced. Review the warnings before acting "
            "on borderline scores."
        )
    checks = sorted({i["check"] for i in errors})
    return (
        "Not suitable for confident scoring: " + ", ".join(checks) + ". "
        "Fix these and re-upload; risk scores derived from this data would be "
        "misleading rather than merely uncertain."
    )


async def assess_dataset_quality(db, dataset_id: Optional[int]) -> Dict[str, object]:
    """Assess a stored dataset, or the built-in fleet when `dataset_id` is None."""
    from sqlalchemy import select

    from app.models.machine import Machine
    from app.models.sensor import SensorReading

    query = select(Machine)
    query = query.where(
        Machine.dataset_id.is_(None) if dataset_id is None else Machine.dataset_id == dataset_id
    )
    machines = (await db.execute(query)).scalars().all()

    rows: List[dict] = []
    for machine in machines:
        readings = (
            await db.execute(
                select(
                    SensorReading.timestamp,
                    SensorReading.vibration,
                    SensorReading.temperature,
                    SensorReading.rpm,
                )
                .where(SensorReading.machine_id == machine.id)
                .order_by(SensorReading.timestamp.asc())
            )
        ).all()
        for timestamp, vibration, temperature, rpm in readings:
            rows.append({
                "machine": machine.label,
                "timestamp": timestamp,
                "vibration": vibration,
                "temperature": temperature,
                "rpm": rpm,
            })

    return assess_quality(rows)
