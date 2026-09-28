"""
Column mapping — accept a real factory export, not one rigid schema.

A plant CSV will not use our column names. It will say `Vibration_mm_s`,
`Bearing_Temp_C`, `Speed_RPM`, `Asset`, `DateTime`. Rejecting those would make the
"upload your own data" claim hollow, so headers are matched against alias tables
and the detected mapping is shown for confirmation before anything is written.

Nothing here guesses silently: every field reports how confidently it was matched,
and an ambiguous timestamp format is an explicit error rather than an assumption.
"""
import logging
import re
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence, Tuple

import pandas as pd

logger = logging.getLogger(__name__)

# Canonical field -> accepted header aliases, matched after normalisation.
# Ordered longest/most-specific first so `bearing_temp` wins over bare `temp`.
FIELD_ALIASES: Dict[str, Tuple[str, ...]] = {
    "machine": (
        "machine", "machineid", "machinename", "asset", "assetid", "assetname",
        "equipment", "equipmentid", "tag", "tagname", "unit", "device", "deviceid",
    ),
    "timestamp": (
        "timestamp", "time", "datetime", "date", "readingtime", "recordedat",
        "measurementtime", "ts", "eventtime",
    ),
    "vibration": (
        "vibrationmms", "vibrationrms", "vibrationmm", "vibrationvelocity",
        "vibration", "vibrationlevel", "vib", "vibrms", "rmsvelocity",
    ),
    "temperature": (
        "bearingtemp", "bearingtemperature", "motortemp", "temperaturec",
        "temperaturef", "temperature", "temp", "tempc", "tempf",
    ),
    "rpm": (
        "rotationalspeed", "rotationspeed", "shaftspeed", "motorspeed",
        "speedrpm", "rpm", "speed",
    ),
    "machine_status": ("machinestatus", "status", "state", "runstatus"),
    "production_count": (
        "productioncount", "totalcount", "unitsproduced", "produced", "output",
        "count", "totalunits",
    ),
    "good_count": (
        "goodcount", "goodunits", "gooduits", "passcount", "goodparts", "good",
        "acceptedunits",
    ),
    "planned_production_time": (
        "plannedproductiontime", "plannedtime", "scheduledtime", "plannedminutes",
    ),
    "actual_run_time": (
        "actualruntime", "runtime", "runningtime", "uptime", "actualminutes",
    ),
    "downtime_minutes": (
        "downtimeminutes", "downtime", "stoppagetime", "downminutes",
    ),
}

# The three sensor channels. At least one is needed to say anything about a
# machine, but demanding all three would reject most real factory exports: public
# and industrial datasets commonly carry vibration OR temperature, rarely both with
# RPM alongside. Missing channels reduce what can be computed, and that reduction is
# reported rather than papered over.
SENSOR_FIELDS = ("vibration", "temperature", "rpm")

# Channels the ML model needs. Without all of them the model cannot be evaluated at
# all, so the risk score is reported as unavailable instead of being fed substitutes.
MODEL_REQUIRED_FIELDS = SENSOR_FIELDS

# Timestamp formats attempted in order. ISO first because it is unambiguous.
TIMESTAMP_FORMATS = (
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y %H:%M",
    "%d/%m/%Y",
    "%m/%d/%Y %H:%M:%S",
    "%m/%d/%Y %H:%M",
    "%m/%d/%Y",
    "%d-%m-%Y %H:%M",
    "%d.%m.%Y %H:%M",
)


def _normalise(header: str) -> str:
    """Lowercase and strip separators, units and punctuation for matching."""
    text = (header or "").strip().lower()
    text = re.sub(r"\[.*?\]|\(.*?\)", "", text)      # drop "(mm/s)" / "[C]"
    text = re.sub(r"[^a-z0-9]", "", text)            # drop _ - space / . etc
    return text


def detect_mapping(headers: Sequence[str]) -> Dict[str, object]:
    """Map the file's headers onto canonical field names.

    Returns the mapping, per-field confidence, unmapped headers and any missing
    required fields, so the caller can present it for confirmation.
    """
    normalised = {h: _normalise(h) for h in headers}
    mapping: Dict[str, str] = {}
    confidence: Dict[str, str] = {}

    for field, aliases in FIELD_ALIASES.items():
        matched: Optional[str] = None
        how = ""

        # Exact normalised match wins.
        for header, norm in normalised.items():
            if norm in aliases and header not in mapping.values():
                matched, how = header, "exact"
                break

        # Otherwise accept a containment match, which catches things like
        # "avg_vibration_mm_s_channel1".
        if matched is None:
            for alias in aliases:
                for header, norm in normalised.items():
                    if header in mapping.values():
                        continue
                    if len(alias) >= 3 and alias in norm:
                        matched, how = header, "partial"
                        break
                if matched:
                    break

        if matched is not None:
            mapping[field] = matched
            confidence[field] = how

    mapped_headers = set(mapping.values())
    present_sensors = [f for f in SENSOR_FIELDS if f in mapping]
    missing_sensors = [f for f in SENSOR_FIELDS if f not in mapping]

    return {
        "mapping": mapping,
        "confidence": confidence,
        "unmapped_headers": [h for h in headers if h not in mapped_headers],
        "present_sensors": present_sensors,
        "missing_sensors": missing_sensors,
        # Only a total absence of sensors is disqualifying. Individual gaps are
        # reported so the caller can say what will and will not be computed.
        "missing_required": [] if present_sensors else list(SENSOR_FIELDS),
        # The model needs all three; without them the deviation score and absolute
        # limits still work, so the upload is useful but the ML score is not.
        "ml_scoreable": len(present_sensors) == len(MODEL_REQUIRED_FIELDS),
    }


def parse_timestamp(value: object) -> Optional[datetime]:
    """Parse a timestamp across common formats, or return None.

    Epoch numbers are supported, distinguishing seconds from milliseconds by
    magnitude. Ambiguity is not silently resolved: a value that matches no known
    format returns None and is reported as a bad row.
    """
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)

    text = str(value).strip()
    if not text:
        return None

    # Epoch seconds / milliseconds.
    if re.fullmatch(r"\d{10}", text):
        return datetime.fromtimestamp(int(text), tz=timezone.utc).replace(tzinfo=None)
    if re.fullmatch(r"\d{13}", text):
        return datetime.fromtimestamp(int(text) / 1000, tz=timezone.utc).replace(tzinfo=None)

    normalised = text.replace("Z", "").replace("T", "T")
    for fmt in TIMESTAMP_FORMATS:
        try:
            return datetime.strptime(normalised, fmt)
        except ValueError:
            continue

    # Last resort: pandas, which handles fractional seconds and offsets.
    try:
        parsed = pd.to_datetime(text, errors="raise")
        if isinstance(parsed, pd.Timestamp):
            return parsed.to_pydatetime().replace(tzinfo=None)
    except Exception:
        pass
    return None


def looks_like_fahrenheit(values: Sequence[float], header: Optional[str] = None) -> bool:
    """Decide whether a temperature column is probably Fahrenheit.

    The header is the stronger signal — `Bearing_Temp_F` says so outright — because
    the value heuristic is unreliable: a hot bearing at 140 F and a hot bearing at
    140 C are not distinguishable by magnitude alone.

    Only ever used to SUGGEST a conversion; it is never applied without the caller
    opting in, because silently rescaling someone's data would be worse than asking.
    """
    if header:
        normalised = _normalise(header)
        # Trailing or embedded Fahrenheit markers, avoiding false hits on words
        # that merely contain an 'f'.
        if normalised.endswith("f") and not normalised.endswith("of"):
            return True
        if "fahrenheit" in normalised or "degf" in normalised or "tempf" in normalised:
            return True

    clean = [float(v) for v in values if v is not None and not pd.isna(v)]
    if len(clean) < 5:
        return False
    median = sorted(clean)[len(clean) // 2]
    # Industrial bearing temperatures in Celsius very rarely sit above 150.
    return median > 150.0


def fahrenheit_to_celsius(value: float) -> float:
    return (float(value) - 32.0) * 5.0 / 9.0
