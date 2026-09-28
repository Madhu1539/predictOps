"""
Recommendation Service — rule-based mapping from failure mode to action (spec §32).
Transparent: recommendations are rule-based guidance, not ML-generated.
"""

RECOMMENDATIONS = {
    "BEARING_DEGRADATION": "Inspect and replace bearing assembly. Check lubrication.",
    "OVERHEATING": "Inspect cooling system and ventilation. Check coolant levels and fan operation.",
    "MOTOR_DEGRADATION": "Inspect motor windings and drive assembly. Check current draw.",
    "MISALIGNMENT": "Inspect shaft alignment. Perform laser alignment check.",
    "GENERAL_MECHANICAL_WEAR": "Perform comprehensive mechanical inspection. Check all wear parts.",
}

PART_MAP = {
    "BEARING_DEGRADATION": "Bearing Assembly",
    "OVERHEATING": "Cooling Fan",
    "MOTOR_DEGRADATION": "Motor Belt",
    "MISALIGNMENT": "Drive Coupling",
    "GENERAL_MECHANICAL_WEAR": "Lubricant",
}


def get_recommendation(failure_mode: str) -> str:
    """Return maintenance recommendation for a given failure mode."""
    return RECOMMENDATIONS.get(failure_mode, RECOMMENDATIONS["GENERAL_MECHANICAL_WEAR"])


def get_required_part(failure_mode: str) -> str:
    """Return the part name required for the given failure mode."""
    return PART_MAP.get(failure_mode, "Lubricant")
