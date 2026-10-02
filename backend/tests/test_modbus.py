"""
Tests for Modbus TCP ingestion.

Two layers, deliberately separated:

  * The decoding and addressing conventions are pure functions, so the part most
    likely to be wrong in a real deployment — which register is which, and what
    it is divided by — is tested without a socket.
  * `poll_once` is tested against a fake client. The point is the behaviour
    around the protocol (short reads, exception responses, containment,
    implausible values), not pymodbus itself.

The framing in `tools/modbus_plc_sim.py` is tested too, because the tool is read
by pymodbus's real client and a malformed frame there would look like an
application bug.
"""
import importlib.util
import pathlib
import struct

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.database import AsyncSessionLocal, init_db
from app.models.dataset import Dataset
from app.models.machine import ORIGIN_EXTERNAL, ORIGIN_SIMULATED, Machine
from app.models.sensor import SensorReading
from app.services.modbus_source import (
    MAX_REGISTERS_PER_READ,
    PollResult,
    SOURCE_NAME,
    build_reading,
    decode_machine_block,
    decode_word,
    ensure_dataset,
    implausible_channels,
    is_unmapped_block,
    machine_addresses,
    parse_machine_labels,
    poll_once,
    read_plan,
)

SCALES = {"vibration": 100.0, "temperature": 10.0, "rpm": 1.0}


# `Machine.label` is a Python property, not a column, so it cannot appear in a
# SQL filter. `get_or_create_machine` stores the user-facing name in
# `display_name` and namespaces `name` as `dsN:label`, so queries use that.
def _by_label(label: str):
    return select(Machine).where(Machine.display_name == label)


# ─── Register decoding ────────────────────────────────────────────────────────

def test_unsigned_register_is_scaled_to_engineering_units():
    """2.45 mm/s travels as the integer 245; the divisor restores it."""
    assert decode_machine_block([245, 615, 1480], SCALES)["vibration"] == 2.45


def test_temperature_is_decoded_signed():
    """Below-zero temperatures arrive as two's complement.

    Nothing on the wire marks a register as signed, so this is a convention the
    adapter has to apply. Reading it unsigned would turn -5.5 C into 6553.1 C.
    """
    raw = 0x10000 - 55  # -5.5 C at a scale of 10
    assert decode_word(raw, True) == -55
    assert decode_machine_block([245, raw, 1480], SCALES)["temperature"] == -5.5


def test_vibration_and_rpm_are_decoded_unsigned():
    """A high bit in these is a large value, not a negative one.

    Treating them as signed would silently turn a severe vibration reading into
    a negative number, which `SensorReadingIn` would then reject as invalid
    rather than flag as alarming.
    """
    assert decode_word(0xC000, False) == 49152
    assert decode_word(0xC000, True) == -16384


def test_register_order_is_vibration_temperature_rpm():
    """The layout is a contract with however the PLC was programmed."""
    decoded = decode_machine_block([100, 200, 300], {"vibration": 1, "temperature": 1, "rpm": 1})
    assert decoded == {"vibration": 100, "temperature": 200, "rpm": 300}


def test_partial_block_yields_none_not_zero():
    """A device publishing only vibration is normal.

    None and 0.0 are very different downstream: one means "not measured", the
    other means "measured as zero", and substituting either way corrupts the
    deviation score.
    """
    assert decode_machine_block([245], SCALES) == {
        "vibration": 2.45, "temperature": None, "rpm": None,
    }


def test_missing_scale_defaults_to_one_rather_than_dividing_by_zero():
    assert decode_machine_block([245, 615, 1480], {})["vibration"] == 245


# ─── Plausibility screening ───────────────────────────────────────────────────

def test_wrong_scale_factor_is_caught():
    """The most likely misconfiguration: a scale off by a power of ten."""
    assert implausible_channels({"vibration": 24500.0}) == ["vibration"]


def test_unconfigured_register_returning_0xffff_is_caught():
    """An address that is not mapped often reads back as all ones."""
    decoded = decode_machine_block([0xFFFF, 100, 100], SCALES)
    assert "vibration" in implausible_channels(decoded)


def test_all_ones_block_is_detected_before_decoding():
    """Decoding hides this, which is why it is checked on the raw words.

    0xFFFF read as a signed temperature at a scale of 10 gives -0.1 C, which
    passes every plausibility range and would be stored as a real measurement
    from a register the device never mapped.
    """
    assert decode_machine_block([0xFFFF] * 3, SCALES)["temperature"] == -0.1
    assert implausible_channels({"temperature": -0.1}) == []
    assert is_unmapped_block([0xFFFF, 0xFFFF, 0xFFFF]) is True


def test_all_zero_block_is_not_treated_as_unmapped():
    """A stopped machine genuinely reads zero RPM; discarding that would hide a
    stoppage, which is information the command centre needs."""
    assert is_unmapped_block([0, 0, 0]) is False


def test_partially_mapped_block_is_kept():
    """Only a wholly unmapped block is discarded; one odd register is handled
    per channel so the machine's other measurements survive."""
    assert is_unmapped_block([0xFFFF, 615, 1480]) is False


def test_empty_block_counts_as_unmapped():
    assert is_unmapped_block([]) is True


def test_realistic_readings_pass():
    assert implausible_channels({"vibration": 2.4, "temperature": 62.0, "rpm": 1480.0}) == []


def test_alarming_but_real_readings_are_not_rejected():
    """Screening catches decode errors, not bad machines.

    11 mm/s is ISO 10816 zone D — genuinely alarming, and exactly the reading
    the system exists to surface. Clamping or dropping it would defeat the point.
    """
    assert implausible_channels({"vibration": 11.0, "temperature": 95.0, "rpm": 1200.0}) == []


def test_none_channels_are_not_screened():
    assert implausible_channels({"vibration": None, "temperature": 62.0}) == []


# ─── Reading construction ────────────────────────────────────────────────────

def test_build_reading_drops_implausible_channel_but_keeps_the_rest():
    """One bad channel should not discard the machine's other measurements."""
    reading = build_reading({"vibration": 24500.0, "temperature": 62.0, "rpm": 1480.0})
    assert reading is not None
    assert reading.vibration is None
    assert reading.temperature == 62.0
    assert reading.rpm == 1480.0


def test_build_reading_returns_none_when_nothing_survives():
    assert build_reading({"vibration": None, "temperature": None, "rpm": None}) is None
    assert build_reading({"vibration": 99999.0, "temperature": 9999.0, "rpm": 999999.0}) is None


def test_build_reading_stamps_arrival_time():
    """Modbus carries no timestamp, so arrival time is the honest answer."""
    reading = build_reading({"vibration": 2.4, "temperature": 62.0, "rpm": 1480.0})
    assert reading.timestamp is not None


# ─── Addressing ──────────────────────────────────────────────────────────────

def test_machine_addresses_are_contiguous_blocks():
    assert machine_addresses(3, 0, 3) == [(0, 3), (3, 3), (6, 3)]


def test_machine_addresses_honour_a_base_offset():
    assert machine_addresses(2, 100, 3) == [(100, 3), (103, 3)]


def test_read_plan_uses_one_request_for_a_small_fleet():
    """A PLC lays data out contiguously, so one round trip is both correct and
    cheaper than one request per machine."""
    assert read_plan(0, 3, 3) == [(0, 9)]


def test_read_plan_never_exceeds_one_modbus_frame():
    """Over-long reads get an exception response, not a short read, so the
    client has to chunk rather than hope the device copes."""
    plan = read_plan(0, 200, 3)
    assert plan
    assert all(count <= MAX_REGISTERS_PER_READ for _, count in plan)


def test_read_plan_chunks_on_machine_boundaries():
    """Splitting mid-machine would require stitching partial blocks together."""
    plan = read_plan(0, 200, 3)
    assert all(count % 3 == 0 for _, count in plan)


def test_read_plan_covers_every_machine_exactly_once():
    plan = read_plan(0, 60, 3)
    assert sum(count for _, count in plan) == 60 * 3
    # Contiguous: each chunk starts where the previous ended.
    cursor = 0
    for address, count in plan:
        assert address == cursor
        cursor += count


def test_read_plan_is_empty_for_no_machines():
    assert read_plan(0, 0, 3) == []


# ─── Configuration parsing ───────────────────────────────────────────────────

def test_machine_labels_are_parsed_and_trimmed():
    assert parse_machine_labels(" A-1 , B-2,C-3 ") == ["A-1", "B-2", "C-3"]


def test_blank_machine_setting_yields_no_machines():
    assert parse_machine_labels("") == []
    assert parse_machine_labels("  ,  ,") == []


def test_machine_label_order_is_preserved():
    """Order maps position to register address, so it is load-bearing."""
    assert parse_machine_labels("C,A,B") == ["C", "A", "B"]


# ─── poll_once against a fake device ─────────────────────────────────────────

class FakeResponse:
    def __init__(self, registers=None, error=False):
        self.registers = registers or []
        self._error = error

    def isError(self):
        return self._error


class FakeClient:
    """Stands in for pymodbus's client, recording what was asked for."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def read_holding_registers(self, address, count=1, device_id=1):
        self.calls.append((address, count, device_id))
        result = self._responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


@pytest_asyncio.fixture
async def db():
    await init_db()
    async with AsyncSessionLocal() as session:
        yield session


@pytest_asyncio.fixture
async def clean_modbus_data():
    """Remove anything this module created, leaving the demo fleet alone."""
    yield
    async with AsyncSessionLocal() as session:
        datasets = (
            await session.execute(select(Dataset).where(Dataset.source == SOURCE_NAME))
        ).scalars().all()
        for dataset in datasets:
            ids = [
                row[0] for row in (
                    await session.execute(
                        select(Machine.id).where(Machine.dataset_id == dataset.id)
                    )
                ).all()
            ]
            if ids:
                await session.execute(
                    delete(SensorReading).where(SensorReading.machine_id.in_(ids))
                )
                await session.execute(delete(Machine).where(Machine.id.in_(ids)))
            await session.execute(delete(Dataset).where(Dataset.id == dataset.id))
        await session.commit()


async def test_poll_stores_a_reading_per_machine(db, clean_modbus_data):
    client = FakeClient([FakeResponse([245, 615, 1480, 180, 550, 2400])])
    result = await poll_once(client, db, ["MB-TEST-1", "MB-TEST-2"])

    assert result.readings_stored == 2
    assert result.machines_read == 2
    assert result.errors == []


async def test_poll_reads_both_machines_in_one_request(db, clean_modbus_data):
    client = FakeClient([FakeResponse([245, 615, 1480, 180, 550, 2400])])
    await poll_once(client, db, ["MB-TEST-1", "MB-TEST-2"])
    assert len(client.calls) == 1
    assert client.calls[0][1] == 6  # two machines x three registers


async def test_poll_tags_readings_with_the_modbus_source(db, clean_modbus_data):
    """`/api/readings/status` groups by source, so this is what makes Modbus
    data distinguishable from the simulator and from CSV uploads."""
    client = FakeClient([FakeResponse([245, 615, 1480])])
    await poll_once(client, db, ["MB-SOURCE-CHECK"])

    machine = (
        await db.execute(_by_label("MB-SOURCE-CHECK"))
    ).scalar_one()
    reading = (
        await db.execute(
            select(SensorReading).where(SensorReading.machine_id == machine.id)
        )
    ).scalars().first()
    assert reading.source == SOURCE_NAME


async def test_modbus_machines_are_external_so_the_simulator_cannot_touch_them(
    db, clean_modbus_data
):
    """The containment boundary that matters most here.

    A machine whose readings come from real hardware must never have synthetic
    readings written over it, or the data provenance becomes unprovable.
    """
    client = FakeClient([FakeResponse([245, 615, 1480])])
    await poll_once(client, db, ["MB-CONTAINMENT"])

    machine = (
        await db.execute(_by_label("MB-CONTAINMENT"))
    ).scalar_one()
    assert machine.data_origin == ORIGIN_EXTERNAL
    assert machine.data_origin != ORIGIN_SIMULATED


async def test_modbus_machines_are_grouped_in_their_own_dataset(db, clean_modbus_data):
    """Not the demo fleet (`dataset_id IS NULL`), so they can be reported on in
    isolation and deleted without touching synthetic data."""
    client = FakeClient([FakeResponse([245, 615, 1480])])
    await poll_once(client, db, ["MB-DATASET"])

    machine = (
        await db.execute(_by_label("MB-DATASET"))
    ).scalar_one()
    assert machine.dataset_id is not None

    dataset = (
        await db.execute(select(Dataset).where(Dataset.id == machine.dataset_id))
    ).scalar_one()
    assert dataset.source == SOURCE_NAME


async def test_polling_twice_reuses_the_same_machine(db, clean_modbus_data):
    """Each poll must not create another machine, or a 5-second interval would
    manufacture thousands of duplicates a day."""
    client = FakeClient([
        FakeResponse([245, 615, 1480]),
        FakeResponse([250, 620, 1478]),
    ])
    await poll_once(client, db, ["MB-REUSE"])
    await poll_once(client, db, ["MB-REUSE"])

    machines = (
        await db.execute(_by_label("MB-REUSE"))
    ).scalars().all()
    assert len(machines) == 1


async def test_exception_response_is_reported_not_stored(db, clean_modbus_data):
    """pymodbus signals a protocol error on the response object rather than by
    raising, so it has to be checked explicitly or garbage gets stored."""
    client = FakeClient([FakeResponse(error=True)])
    result = await poll_once(client, db, ["MB-EXC"])

    assert result.readings_stored == 0
    assert result.errors
    assert "exception response" in result.errors[0]


async def test_short_read_is_rejected_rather_than_misaligned(db, clean_modbus_data):
    """Accepting a short read would shift every subsequent machine's registers,
    so one machine's temperature becomes another's vibration."""
    client = FakeClient([FakeResponse([245, 615])])  # asked for 3
    result = await poll_once(client, db, ["MB-SHORT"])

    assert result.readings_stored == 0
    assert result.errors
    assert "short read" in result.errors[0]


async def test_transport_error_is_captured_not_raised(db, clean_modbus_data):
    """A dropped connection is routine on a plant network and must not escape
    into the caller's loop."""
    client = FakeClient([ConnectionResetError("device went away")])
    result = await poll_once(client, db, ["MB-DROP"])

    assert result.readings_stored == 0
    assert result.errors
    assert "ConnectionResetError" in result.errors[0]


async def test_one_implausible_machine_does_not_block_the_others(db, clean_modbus_data):
    """A single unmapped block should cost one machine, not the whole poll."""
    client = FakeClient([FakeResponse([0xFFFF, 0xFFFF, 0xFFFF, 180, 550, 2400])])
    result = await poll_once(client, db, ["MB-BAD", "MB-GOOD"])

    assert result.rejected == 1
    assert result.readings_stored == 1

    good = (
        await db.execute(_by_label("MB-GOOD"))
    ).scalar_one_or_none()
    assert good is not None
    bad = (
        await db.execute(_by_label("MB-BAD"))
    ).scalar_one_or_none()
    assert bad is None, "a rejected machine should not be provisioned"


async def test_dataset_is_created_once_and_then_reused(db, clean_modbus_data):
    first = await ensure_dataset(db, "Modbus reuse check")
    second = await ensure_dataset(db, "Modbus reuse check")
    assert first.id == second.id
    await db.execute(delete(Dataset).where(Dataset.id == first.id))
    await db.commit()


def test_poll_result_defaults_to_an_empty_error_list():
    """A mutable default would be shared across every poll."""
    assert PollResult().errors == []
    assert PollResult().errors is not PollResult().errors


# ─── The stand-in device's framing ───────────────────────────────────────────

def _load_sim():
    """Load the tool by path; `tools/` is not an importable package."""
    path = pathlib.Path(__file__).resolve().parents[2] / "tools" / "modbus_plc_sim.py"
    spec = importlib.util.spec_from_file_location("modbus_plc_sim", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _request(txn, unit, func_code, address, count):
    body = struct.pack(">BBHH", unit, func_code, address, count)
    return struct.pack(">HHH", txn, 0, len(body)) + body


def test_simulator_file_exists_and_loads():
    assert _load_sim() is not None


def test_simulator_encodes_negative_temperature_as_twos_complement():
    """The simulator and the adapter have to agree, or the demo shows nonsense."""
    sim = _load_sim()
    assert sim.encode(-5.5, 10.0) == 0x10000 - 55
    assert decode_word(sim.encode(-5.5, 10.0), True) / 10.0 == -5.5


def test_simulator_round_trips_through_the_adapters_decoder():
    """The real contract: what the device encodes is what the adapter decodes."""
    sim = _load_sim()
    words = [
        sim.encode(2.45, sim.VIBRATION_SCALE),
        sim.encode(61.5, sim.TEMPERATURE_SCALE),
        sim.encode(1480, sim.RPM_SCALE),
    ]
    decoded = decode_machine_block(words, SCALES)
    assert decoded["vibration"] == 2.45
    assert decoded["temperature"] == 61.5
    assert decoded["rpm"] == 1480


def test_simulator_clamps_rather_than_overflowing():
    sim = _load_sim()
    assert sim.encode(1e9, 100.0) == 32767
    assert sim.encode(-1e9, 100.0) == (-32768 & 0xFFFF)


def test_simulator_answers_a_valid_read_with_the_right_frame():
    sim = _load_sim()
    registers = sim.HoldingRegisters([111, 222, 333])
    frame = sim.handle_request(_request(7, 1, 3, 0, 3), registers)

    txn, proto, length = struct.unpack(">HHH", frame[:6])
    assert txn == 7            # echoed, so a client can match request to reply
    assert proto == 0
    assert frame[6] == 1       # unit id
    assert frame[7] == 3       # function code
    assert frame[8] == 6       # byte count: three registers
    assert length == len(frame) - 6
    assert list(struct.unpack(">HHH", frame[9:15])) == [111, 222, 333]


def test_simulator_rejects_an_out_of_range_address():
    """A real device answers exception code 2, not a truncated frame."""
    sim = _load_sim()
    registers = sim.HoldingRegisters([1, 2, 3])
    frame = sim.handle_request(_request(1, 1, 3, 2, 5), registers)
    assert frame[7] == 3 | 0x80
    assert frame[8] == sim.EXC_ILLEGAL_ADDRESS


def test_simulator_rejects_an_unsupported_function_code():
    sim = _load_sim()
    registers = sim.HoldingRegisters([1, 2, 3])
    frame = sim.handle_request(_request(1, 1, 6, 0, 1), registers)
    assert frame[7] == 6 | 0x80
    assert frame[8] == sim.EXC_ILLEGAL_FUNCTION


def test_simulator_rejects_a_read_larger_than_one_frame():
    sim = _load_sim()
    registers = sim.HoldingRegisters(list(range(300)))
    frame = sim.handle_request(_request(1, 1, 3, 0, 200), registers)
    assert frame[8] == sim.EXC_ILLEGAL_VALUE


def test_simulator_register_count_matches_its_machine_list():
    sim = _load_sim()
    assert len(sim.build_block(0)) == len(sim.MACHINES) * sim.REGISTERS_PER_MACHINE


def test_simulator_degrading_machine_actually_degrades():
    """Without this the demo would show a flat line and prove nothing."""
    sim = _load_sim()
    index = next(i for i, m in enumerate(sim.MACHINES) if m[4])
    offset = index * sim.REGISTERS_PER_MACHINE
    early = sim.build_block(0)[offset]
    later = sim.build_block(100)[offset]
    assert later > early


def test_simulator_healthy_machines_stay_in_a_sane_band():
    sim = _load_sim()
    for step in (0, 50, 100):
        words = sim.build_block(step)
        for index, machine in enumerate(sim.MACHINES):
            if machine[4]:
                continue
            vibration = words[index * sim.REGISTERS_PER_MACHINE] / sim.VIBRATION_SCALE
            assert abs(vibration - machine[1]) < 1.0
