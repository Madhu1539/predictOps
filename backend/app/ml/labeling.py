"""
Failure event definitions and label construction (spec §17, §19).

This module is the single source of truth for the synthetic failure scenarios,
shared by the data generator and the training pipeline.

Why it exists
-------------
The prediction target is `will_fail_within_7_days`. A label must therefore be
derived from *when a machine actually failed*, not from the sensor signals that
the model receives as features. Deriving labels from rolling sensor statistics
that are also fed in as features is target leakage: the model would simply be
re-reading its own inputs, and the resulting metrics would be meaningless.

Here the failure event day is a property of the generated scenario, and the
label is a pure function of the timestamp's distance from that event. Features
stay strictly on the sensor/maintenance side of the boundary.
"""
from typing import Dict, Optional, Tuple

import pandas as pd

# Prediction horizon for the target variable (spec §17).
FAILURE_HORIZON_DAYS = 7

# Degradation scenarios injected by the generator:
#   machine_name -> (failure_mode, degradation_start_day, severity_ramp)
#
# M-102 is the demo machine (spec §49). Its start day is chosen so that the
# failure event lands just beyond the end of the 90-day dataset, which puts
# "now" inside the 7-day pre-failure window and lets the model legitimately
# predict imminent failure. The score still comes from the model.
FAILURE_SCENARIOS: Dict[str, Tuple[str, int, str]] = {
    "M-102": ("BEARING_DEGRADATION", 68, "severe"),    # demo — fails day 93
    "M-104": ("OVERHEATING", 60, "moderate"),
    "HP-201": ("MOTOR_DEGRADATION", 50, "moderate"),
    "HP-203": ("MISALIGNMENT", 65, "severe"),
    "CM-301": ("BEARING_DEGRADATION", 40, "moderate"),
    "CM-305": ("GENERAL_MECHANICAL_WEAR", 70, "mild"),
    "IP-401": ("OVERHEATING", 45, "severe"),
    "IP-403": ("MOTOR_DEGRADATION", 60, "moderate"),
}

# How long degradation runs before the machine actually fails. A severe ramp
# reaches failure sooner than a mild one.
TIME_TO_FAILURE_DAYS: Dict[str, int] = {
    "severe": 25,
    "moderate": 30,
    "mild": 35,
}


def get_failure_day(machine_name: str) -> Optional[int]:
    """Day index (from the start of the dataset) on which the machine fails.

    Returns None for machines with no injected scenario. The value may exceed
    the dataset length, meaning the failure is still in the future — readings
    inside the preceding 7 days are still labelled positive.
    """
    scenario = FAILURE_SCENARIOS.get(machine_name)
    if scenario is None:
        return None

    _failure_mode, start_day, severity = scenario
    return start_day + TIME_TO_FAILURE_DAYS[severity]


def label_readings(
    timestamps: pd.Series,
    machine_start: pd.Timestamp,
    machine_name: str,
    total_days: int,
) -> pd.Series:
    """Label each reading 1 if it falls in the 7 days before the failure event.

    `machine_start` is the first reading timestamp for the machine, which anchors
    the day index used by the scenario definitions.
    """
    labels = pd.Series(0, index=timestamps.index, dtype=int)

    failure_day = get_failure_day(machine_name)
    if failure_day is None:
        return labels

    # The target is "fails within 7 days", so readings are positive whenever
    # they fall inside the pre-failure window — even if the failure event itself
    # lands just past the end of the dataset. Only skip when the window starts
    # after the data ends, meaning nothing observed is within 7 days of failure.
    if failure_day - FAILURE_HORIZON_DAYS >= total_days:
        return labels

    failure_ts = machine_start + pd.Timedelta(days=failure_day)
    window_start = failure_ts - pd.Timedelta(days=FAILURE_HORIZON_DAYS)

    in_window = (timestamps > window_start) & (timestamps <= failure_ts)
    labels.loc[in_window] = 1
    return labels
