"""
Modbus TCP ingestion — a real industrial protocol client.

This is the first ingestion path that speaks a protocol an actual plant exposes,
rather than generating readings (`live_feed.py`) or accepting a file
(`routes/datasets.py`). Point `MODBUS_HOST` at a real PLC, VFD or gateway and
this reads from it unchanged; nothing here is specific to the bundled demo
server in `tools/modbus_plc_sim.py`.

Why Modbus TCP: it is the protocol most widely present on existing factory
floors. It is also deliberately primitive — a flat array of unsigned 16-bit
registers with no type information, no units, and no timestamps. That shapes
everything below:

  * **Scaling is a convention, not metadata.** A vibration of 2.45 mm/s is
    transmitted as the integer 245. The scale factor lives in the PLC
    programmer's head and in a commissioning document, so it has to be
    configuration here. Guessing it wrong silently changes every downstream
    score by an order of magnitude, which is why the scales are explicit
    settings rather than constants.
  * **Sign is a convention too.** Registers are unsigned on the wire. A
    temperature below zero is conventionally sent as two's complement, so
    temperature is decoded signed while vibration and RPM are not — a negative
    vibration or RPM is a fault, not a reading.
  * **There is no timestamp**, so arrival time is used. This is honest for a
    poll-based protocol: the reading is "what the register held when we asked".

Readings land through `_persist_and_score`, the same function the CSV upload
path uses, so Modbus data is scored by exactly the code that scores everything
else. Machines are created through `get_or_create_machine`, which marks them
`external` — that keeps the simulator from ever writing to a machine whose
readings came from real hardware.
"""
import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.dataset import Dataset
from app.schemas import SensorReadingIn

logger = logging.getLogger(__name__)

settings = get_settings()

SOURCE_NAME = "modbus"

# Offsets within each machine's register block, and whether the value is signed.
# Order matters: it defines the on-wire layout this client expects a device to
# publish, and it is documented in the README so a PLC can be programmed to match.
REGISTER_LAYOUT: Tuple[Tuple[str, int, bool], ...] = (
    ("vibration", 0, False),
    ("temperature", 1, True),
    ("rpm", 2, False),
)

# A single Modbus read is bounded by the protocol's frame size. Exceeding it
# produces an exception response rather than a short read, so requests are
# chunked instead of relying on the device to cope.
MAX_REGISTERS_PER_READ = 125

# Rejected rather than stored. These are not tight engineering limits — the
# absolute-limit checks in `anomaly_service` do that work, and clamping here
# would hide a genuinely alarming reading. These catch a *decode* that has gone
# wrong: a wrong scale factor, a register holding a status word, or an
# unconfigured address returning 0xFFFF.
PLAUSIBLE_RANGES: Dict[str, Tuple[float, float]] = {
    "vibration": (0.0, 200.0),      # mm/s; ISO 10816 zone D starts ~11 mm/s
    "temperature": (-50.0, 500.0),  # matches SensorReadingIn's own bounds
    "rpm": (0.0, 30000.0),
}


def decode_word(raw: int, signed: bool) -> int:
    """Interpret one 16-bit register.

    pymodbus returns registers as Python ints already masked to 16 bits. Two's
    complement has to be applied by the caller, because the wire format carries
    no indication of signedness.
    """
    value = int(raw) & 0xFFFF
    if signed and value >= 0x8000:
        value -= 0x10000
    return value


def decode_machine_block(words: Sequence[int], scales: Dict[str, float]) -> Dict[str, Optional[float]]:
    """Turn one machine's register block into engineering units.

    Kept free of I/O so the convention that actually matters — which register is
    which, and what it is divided by — can be tested without a device.

    A short block yields `None` for the channels it does not cover rather than
    raising. A device that publishes only vibration is normal, and
    `SensorReadingIn` accepts partial channels by design.
    """
    decoded: Dict[str, Optional[float]] = {}
    for field, offset, signed in REGISTER_LAYOUT:
        if offset >= len(words):
            decoded[field] = None
            continue
        scale = scales.get(field) or 1.0
        decoded[field] = decode_word(words[offset], signed) / scale
    return decoded


def implausible_channels(values: Dict[str, Optional[float]]) -> List[str]:
    """Names of channels whose decoded value cannot be a real measurement."""
    bad = []
    for field, value in values.items():
        if value is None:
            continue
        low, high = PLAUSIBLE_RANGES.get(field, (float("-inf"), float("inf")))
        if not (low <= value <= high):
            bad.append(field)
    return bad


def is_unmapped_block(words: Sequence[int]) -> bool:
    """True when a register block looks like an address the device does not serve.

    Checked on the raw words, before scaling, because this is a wire-level
    condition rather than an engineering one — and because decoding hides it.
    An all-ones block scaled as a signed temperature gives -0.1 °C, which passes
    every plausibility check and would be stored as a real measurement.

    An all-zero block is deliberately NOT treated this way: a stopped machine
    genuinely reads zero RPM, and discarding that would hide a stoppage.
    """
    if not words:
        return True
    return all((int(word) & 0xFFFF) == 0xFFFF for word in words)


def machine_addresses(
    machine_count: int, base_address: int, stride: int
) -> List[Tuple[int, int]]:
    """`(start_address, register_count)` per machine, in configured order."""
    return [
        (base_address + index * stride, stride)
        for index in range(machine_count)
    ]


def read_plan(
    base_address: int, machine_count: int, stride: int
) -> List[Tuple[int, int]]:
    """Chunk the whole span into requests that fit one Modbus frame.

    Machines are read as one contiguous span where possible because that is how
    a PLC is normally laid out and it costs one round trip instead of N. The
    chunk boundary is aligned to `stride` so a machine's registers are never
    split across two responses, which would otherwise require stitching partial
    blocks back together.
    """
    if machine_count <= 0 or stride <= 0:
        return []
    machines_per_chunk = max(1, MAX_REGISTERS_PER_READ // stride)
    plan = []
    for first in range(0, machine_count, machines_per_chunk):
        count = min(machines_per_chunk, machine_count - first)
        plan.append((base_address + first * stride, count * stride))
    return plan


@dataclass
class PollResult:
    """What one polling cycle did. Surfaced for tests and for logging."""

    machines_read: int = 0
    readings_stored: int = 0
    rejected: int = 0
    errors: List[str] = None

    def __post_init__(self):
        if self.errors is None:
            self.errors = []


def parse_machine_labels(raw: str) -> List[str]:
    """Machine labels from the comma-separated setting, order preserved.

    Order is significant: it maps position to register address, so reordering
    this setting repoints every machine at a different block.
    """
    return [label.strip() for label in (raw or "").split(",") if label.strip()]


async def ensure_dataset(db: AsyncSession, name: str) -> Dataset:
    """Find or create the dataset that groups machines from this device.

    A dataset rather than the demo fleet (`dataset_id IS NULL`) for two reasons:
    it can be reported on in isolation via `/api/report?dataset_id=`, and it can
    be deleted without touching the synthetic fleet.
    """
    existing = (
        await db.execute(select(Dataset).where(Dataset.name == name))
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    dataset = Dataset(
        name=name,
        description="Readings polled from a Modbus TCP device.",
        source=SOURCE_NAME,
        status="ready",
    )
    db.add(dataset)
    await db.commit()
    await db.refresh(dataset)
    logger.info("Created Modbus dataset %r (id=%s)", name, dataset.id)
    return dataset


def build_reading(values: Dict[str, Optional[float]]) -> Optional[SensorReadingIn]:
    """Validate one decoded block into a reading, or None if unusable.

    Returns None rather than raising for the two expected cases — every channel
    absent, or a value outside what a sensor can produce — because one bad
    machine in a block should not abort the other machines in the same poll.
    """
    bad = implausible_channels(values)
    for field in bad:
        values[field] = None

    if not any(v is not None for v in values.values()):
        return None

    try:
        return SensorReadingIn(
            timestamp=datetime.utcnow(),
            vibration=values.get("vibration"),
            temperature=values.get("temperature"),
            rpm=values.get("rpm"),
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Modbus reading rejected by schema: %s", exc)
        return None


async def poll_once(client, db: AsyncSession, labels: Sequence[str]) -> PollResult:
    """Read every configured machine once and store what came back.

    `client` is anything exposing pymodbus's `read_holding_registers`, which is
    what makes this testable against a fake without a socket.
    """
    # Imported here, not at module scope: `routes.ingest` imports from
    # `app.services`, so a top-level import would close an import cycle.
    from app.routes.ingest import _persist_and_score
    from app.services.dataset_service import get_or_create_machine

    result = PollResult()
    stride = settings.modbus_registers_per_machine
    scales = {
        "vibration": settings.modbus_vibration_scale,
        "temperature": settings.modbus_temperature_scale,
        "rpm": settings.modbus_rpm_scale,
    }

    words: List[int] = []
    for address, count in read_plan(settings.modbus_base_address, len(labels), stride):
        try:
            response = await client.read_holding_registers(
                address, count=count, device_id=settings.modbus_device_id
            )
        except Exception as exc:
            result.errors.append(f"read at {address} failed: {type(exc).__name__}")
            return result

        # pymodbus signals a protocol-level exception response on the object
        # rather than by raising, so this has to be checked explicitly.
        if response is None or getattr(response, "isError", lambda: False)():
            result.errors.append(f"device returned an exception response at {address}")
            return result

        registers = list(getattr(response, "registers", []) or [])
        if len(registers) < count:
            result.errors.append(
                f"short read at {address}: wanted {count}, got {len(registers)}"
            )
            return result
        words.extend(registers)

    dataset = await ensure_dataset(db, settings.modbus_dataset_name)

    for index, label in enumerate(labels):
        block = words[index * stride:(index + 1) * stride]
        if is_unmapped_block(block):
            result.rejected += 1
            continue
        values = decode_machine_block(block, scales)
        reading = build_reading(values)
        if reading is None:
            result.rejected += 1
            continue

        machine, _created = await get_or_create_machine(
            db, label, dataset.id, machine_type=settings.modbus_machine_type
        )
        await _persist_and_score(db, machine, [reading], SOURCE_NAME)
        result.machines_read += 1
        result.readings_stored += 1

    dataset.row_count = (dataset.row_count or 0) + result.readings_stored
    dataset.machine_count = len(labels)
    await db.commit()
    return result


async def modbus_poll_loop() -> None:
    """Poll the configured device until cancelled.

    Deliberately resilient: a PLC that is switched off, rebooting, or behind a
    dropped VPN is an ordinary condition on a plant network, not a reason to
    take the API down with it. Connection failures back off and retry, and the
    loop logs the first failure then goes quiet so a disconnected device cannot
    fill the log.
    """
    labels = parse_machine_labels(settings.modbus_machines)
    if not labels:
        logger.warning(
            "MODBUS_ENABLED is true but MODBUS_MACHINES is empty; nothing to poll."
        )
        return

    from pymodbus.client import AsyncModbusTcpClient

    from app.database import AsyncSessionLocal

    host, port = settings.modbus_host, settings.modbus_port
    logger.info(
        "Modbus TCP poller starting: %s:%s device_id=%s machines=%s every %ss",
        host, port, settings.modbus_device_id, len(labels),
        settings.modbus_poll_seconds,
    )

    backoff = 1.0
    complained = False

    while True:
        client = AsyncModbusTcpClient(host, port=port, timeout=3)
        try:
            connected = await client.connect()
            if not connected:
                raise ConnectionError(f"could not connect to {host}:{port}")

            backoff = 1.0
            if complained:
                logger.info("Modbus device at %s:%s is reachable again.", host, port)
                complained = False

            while client.connected:
                async with AsyncSessionLocal() as db:
                    outcome = await poll_once(client, db, labels)

                if outcome.errors:
                    logger.warning("Modbus poll: %s", "; ".join(outcome.errors))
                elif outcome.rejected:
                    logger.info(
                        "Modbus poll: stored %s, rejected %s as implausible "
                        "(check the scale factors)",
                        outcome.readings_stored, outcome.rejected,
                    )
                else:
                    logger.debug("Modbus poll: stored %s", outcome.readings_stored)

                await asyncio.sleep(settings.modbus_poll_seconds)

        except asyncio.CancelledError:
            logger.info("Modbus poller stopping.")
            raise
        except Exception as exc:
            if not complained:
                logger.warning(
                    "Modbus device at %s:%s unreachable (%s). Retrying; "
                    "further failures will be logged at debug.",
                    host, port, exc,
                )
                complained = True
            else:
                logger.debug("Modbus retry failed: %s", exc)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)
        finally:
            try:
                client.close()
            except Exception:  # pragma: no cover - close is best effort
                pass
