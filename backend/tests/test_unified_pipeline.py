"""
Tests for the unified data pipeline: uploaded data and the built-in demo fleet
travel the same path, and the simulator is never allowed to touch external data.
"""
from datetime import datetime, timedelta

import pandas as pd
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select

from app.database import AsyncSessionLocal, init_db
from app.main import app
from app.models.alert import Alert
from app.models.dataset import Dataset
from app.models.machine import ORIGIN_EXTERNAL, ORIGIN_SIMULATED, Machine
from app.models.sensor import SensorReading

CSV = """Asset,DateTime,Vibration_mm_s (RMS),Bearing_Temp_C,Shaft Speed [RPM],Units Produced,Good Units,Notes
PUMP-A,2026-03-01 08:00,1.80,64.0,1480,610,605,ok
PUMP-A,2026-03-01 09:00,1.85,64.5,1478,612,606,ok
PUMP-A,2026-03-01 10:00,1.82,65.0,1479,611,604,ok
PUMP-A,2026-03-01 11:00,1.90,66.0,1477,609,602,ok
PRESS-7,2026-03-01 08:00,3.40,70.0,820,400,395,ok
PRESS-7,2026-03-01 09:00,3.60,71.5,815,398,390,ok
"""


@pytest_asyncio.fixture
async def client():
    await init_db()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def dataset(client):
    """A dataset that is torn down afterwards, leaving the demo fleet untouched."""
    created = (await client.post("/api/datasets", json={"name": "Test Upload"})).json()
    yield created
    await client.delete(f"/api/datasets/{created['id']}")


def _stamp(offset_hours: int) -> str:
    """Hourly ISO timestamps that roll over the day boundary correctly."""
    return (datetime(2026, 3, 1, 8, 0) + timedelta(hours=offset_hours)).strftime("%Y-%m-%d %H:%M")


# ─── Column mapping ───────────────────────────────────────────────────────────

def test_detects_real_world_column_names():
    from app.services.column_mapping import detect_mapping

    detected = detect_mapping([
        "Asset", "DateTime", "Vibration_mm_s (RMS)", "Bearing_Temp_C",
        "Shaft Speed [RPM]", "Units Produced", "Good Units", "Notes",
    ])
    mapping = detected["mapping"]
    assert mapping["machine"] == "Asset"
    assert mapping["timestamp"] == "DateTime"
    assert mapping["vibration"] == "Vibration_mm_s (RMS)"
    assert mapping["temperature"] == "Bearing_Temp_C"
    assert mapping["rpm"] == "Shaft Speed [RPM]"
    assert detected["missing_required"] == []
    # A column we do not understand must be reported, not silently consumed.
    assert "Notes" in detected["unmapped_headers"]


def test_missing_required_columns_are_reported():
    from app.services.column_mapping import detect_mapping

    detected = detect_mapping(["machine", "timestamp", "pressure"])
    assert set(detected["missing_required"]) == {"vibration", "temperature", "rpm"}


@pytest.mark.parametrize("raw,expected_year", [
    ("2026-03-01T08:30:00", 2026),
    ("2026-03-01 08:30", 2026),
    ("01/03/2026 08:30", 2026),
    ("2026-03-01", 2026),
    ("1772000000", 2026),        # epoch seconds
    ("1772000000000", 2026),     # epoch milliseconds
])
def test_parses_common_timestamp_formats(raw, expected_year):
    from app.services.column_mapping import parse_timestamp

    parsed = parse_timestamp(raw)
    assert parsed is not None, raw
    assert parsed.year == expected_year


def test_unparseable_timestamp_returns_none_rather_than_guessing():
    from app.services.column_mapping import parse_timestamp

    assert parse_timestamp("not-a-date") is None
    assert parse_timestamp("") is None
    assert parse_timestamp(None) is None


def test_fahrenheit_detected_from_header():
    """The header is the reliable signal; magnitude alone cannot distinguish a hot
    bearing in Celsius from a warm one in Fahrenheit."""
    from app.services.column_mapping import looks_like_fahrenheit

    assert looks_like_fahrenheit([140.0, 142.0, 143.0, 144.0, 145.0], header="Bearing_Temp_F")
    assert not looks_like_fahrenheit([64.0, 65.0, 66.0, 64.5, 65.5], header="Bearing_Temp_C")


# ─── Upload flow ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_preview_writes_nothing(client, dataset):
    before = (await client.get("/api/machines", params={"dataset_id": dataset["id"]})).json()
    preview = (await client.post(
        f"/api/datasets/{dataset['id']}/preview",
        content=CSV.encode(), headers={"Content-Type": "text/csv"},
    )).json()
    after = (await client.get("/api/machines", params={"dataset_id": dataset["id"]})).json()

    assert preview["ready_to_commit"] is True
    assert sorted(preview["detected_machines"]) == ["PRESS-7", "PUMP-A"]
    assert len(before) == len(after) == 0, "preview must not create anything"


@pytest.mark.asyncio
async def test_upload_provisions_machines_and_scores_them(client, dataset):
    result = (await client.post(
        f"/api/datasets/{dataset['id']}/upload",
        params={"machine_type": "Industrial Pump"},
        content=CSV.encode(), headers={"Content-Type": "text/csv"},
    )).json()

    assert result["machines_created"] == 2
    assert result["readings_accepted"] == 6
    assert sorted(result["machines"]) == ["PRESS-7", "PUMP-A"]

    machines = (await client.get("/api/machines", params={"dataset_id": dataset["id"]})).json()
    assert len(machines) == 2
    # Internal dataset-scoped names must never reach the user.
    for machine in machines:
        assert not machine["name"].startswith("ds")
        assert machine["latest_risk"] is not None


@pytest.mark.asyncio
async def test_bad_rows_rejected_individually(client, dataset):
    messy = CSV + "PUMP-A,not-a-date,1.9,66,1470,600,590,bad\nPUMP-A,2026-03-01 13:00,NOPE,66,1470,600,590,bad\n"
    result = (await client.post(
        f"/api/datasets/{dataset['id']}/upload",
        content=messy.encode(), headers={"Content-Type": "text/csv"},
    )).json()
    assert result["readings_accepted"] == 6
    assert result["readings_rejected"] == 2
    assert {e["row"] for e in result["errors"]} == {8, 9}


@pytest.mark.asyncio
async def test_upload_rejects_file_with_no_sensor_columns_at_all(client, dataset):
    """One sensor is enough, but none is not: there is nothing to assess."""
    response = await client.post(
        f"/api/datasets/{dataset['id']}/upload",
        content=b"machine,timestamp,pressure\nA,2026-03-01 08:00,4\n",
        headers={"Content-Type": "text/csv"},
    )
    assert response.status_code == 400
    assert "CSV_NO_SENSOR_COLUMNS" in response.json()["detail"]


@pytest.mark.asyncio
async def test_upload_accepts_a_single_sensor_channel(client, dataset):
    """Real plants are rarely fully instrumented. A temperature-only export — the
    shape of most public compressor datasets — must load and be assessed."""
    temperature_only = "asset,datetime,bearing_temp_c\n" + "".join(
        f"COMP-1,{_stamp(i)},{62 + i * 0.4:.1f}\n" for i in range(30)
    )
    preview = (await client.post(
        f"/api/datasets/{dataset['id']}/preview",
        content=temperature_only.encode(), headers={"Content-Type": "text/csv"},
    )).json()

    assert preview["ready_to_commit"] is True
    assert preview["present_sensors"] == ["temperature"]
    assert set(preview["missing_sensors"]) == {"vibration", "rpm"}
    assert preview["ml_scoreable"] is False
    assert any("ML risk score" in note for note in preview["notes"])

    result = (await client.post(
        f"/api/datasets/{dataset['id']}/upload",
        content=temperature_only.encode(), headers={"Content-Type": "text/csv"},
    )).json()
    assert result["readings_accepted"] == 30
    assert result["readings_rejected"] == 0

    report = (await client.get("/api/report", params={"dataset_id": dataset["id"]})).json()
    machine = report["machines"][0]
    # No ML score, but the machine is still assessed.
    assert machine["ml_risk_score"] is None
    assert machine["deviation_score"] is not None
    assert machine["current"]["temperature"] is not None
    assert machine["current"]["vibration"] is None


@pytest.mark.asyncio
async def test_absent_sensor_is_stored_as_null_not_substituted(client, dataset):
    """A substituted value would be indistinguishable from a real measurement."""
    vibration_only = "asset,datetime,vibration_mm_s\n" + "".join(
        f"BRG-1,{_stamp(i)},{1.6 + i * 0.05:.2f}\n" for i in range(26)
    )
    await client.post(
        f"/api/datasets/{dataset['id']}/upload",
        content=vibration_only.encode(), headers={"Content-Type": "text/csv"},
    )
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(SensorReading.vibration, SensorReading.temperature, SensorReading.rpm)
            .join(Machine, Machine.id == SensorReading.machine_id)
            .where(Machine.dataset_id == dataset["id"])
        )).all()

    assert rows
    for vibration, temperature, rpm in rows:
        assert vibration is not None
        assert temperature is None, "temperature was substituted rather than left absent"
        assert rpm is None, "rpm was substituted rather than left absent"


@pytest.mark.asyncio
async def test_single_sensor_machine_still_flagged_by_absolute_limits(client, dataset):
    """The safety net has to survive partial instrumentation: a temperature-only
    machine running far too hot must not be reported as Normal just because the ML
    model could not be evaluated."""
    hot_only = "asset,datetime,bearing_temp_c\n" + "".join(
        f"HOT-ONLY,{_stamp(i)},{112 + i * 0.5:.1f}\n" for i in range(26)
    )
    await client.post(
        f"/api/datasets/{dataset['id']}/upload",
        content=hot_only.encode(), headers={"Content-Type": "text/csv"},
    )
    report = (await client.get("/api/report", params={"dataset_id": dataset["id"]})).json()
    machine = report["machines"][0]

    assert machine["ml_risk_score"] is None
    assert machine["severity"] != "Normal", "a machine above 110 C was reported Normal"
    assert machine["absolute_concerns"], "published limits should still apply"


def test_confidence_reports_model_unavailable_for_partial_sensors():
    from app.services.anomaly_service import assess_confidence

    partial = assess_confidence(
        reading_count=5000, machine_type="CNC Machine",
        reference_source="derived_baseline", maintenance_available=True,
        missing_sensors=["vibration", "rpm"],
    )
    assert partial["ml_available"] is False
    assert partial["confidence"] == "not_applicable"
    assert "deviation" in partial["scoring_basis"]
    assert any("cannot be evaluated" in r for r in partial["reasons"])


def test_deviation_uses_only_the_channels_present():
    """Weights are renormalised across available sensors, so a missing channel does
    not dilute the score toward zero and look reassuring."""
    from app.services.anomaly_service import deviation_score

    reference = {"vibration": None, "temperature": 60.0, "rpm": None}
    hot = deviation_score(
        {"vibration": None, "temperature": 90.0, "rpm": None}, reference
    )
    assert hot["score"] > 50.0
    assert hot["dominant_sensor"] == "temperature"
    assert [c["available"] for c in hot["components"] if c["sensor"] == "vibration"] == [False]


def test_at_least_one_sensor_is_required_on_a_reading():
    import pytest as _pytest

    from app.schemas import SensorReadingIn

    # One channel is enough.
    assert SensorReadingIn(temperature=64.0).vibration is None
    # None at all is not.
    with _pytest.raises(Exception):
        SensorReadingIn(machine_status="Running")


@pytest.mark.asyncio
async def test_uploaded_machine_name_can_collide_with_demo_fleet(client, dataset):
    """A judge uploading M-102 must not clash with the demo M-102."""
    collide = "machine,timestamp,vibration,temperature,rpm\nM-102,2026-03-01 08:00,2.0,65,1450\n"
    result = (await client.post(
        f"/api/datasets/{dataset['id']}/upload",
        content=collide.encode(), headers={"Content-Type": "text/csv"},
    )).json()
    assert result["machines_created"] == 1

    async with AsyncSessionLocal() as db:
        demo = (await db.execute(
            select(Machine).where(Machine.name == "M-102").where(Machine.dataset_id.is_(None))
        )).scalar_one()
        uploaded = (await db.execute(
            select(Machine).where(Machine.dataset_id == dataset["id"])
        )).scalar_one()

    assert demo.id != uploaded.id
    assert uploaded.label == "M-102"           # what the user sees
    assert uploaded.name != demo.name          # what is stored


# ─── Isolation: the guarantee that makes any of this trustworthy ──────────────

@pytest.mark.asyncio
async def test_simulator_never_writes_to_external_machines(client, dataset):
    """The single most important property: uploaded data is never extended with
    fabricated readings. Without this, a judge's real data becomes a fake story."""
    import asyncio

    from app.services.live_feed import live_feed_loop

    await client.post(
        f"/api/datasets/{dataset['id']}/upload",
        content=CSV.encode(), headers={"Content-Type": "text/csv"},
    )

    async with AsyncSessionLocal() as db:
        machine_ids = [
            row[0] for row in (await db.execute(
                select(Machine.id).where(Machine.dataset_id == dataset["id"])
            )).all()
        ]
        before = (await db.execute(
            select(func.count()).select_from(SensorReading)
            .where(SensorReading.machine_id.in_(machine_ids))
        )).scalar_one()

    task = asyncio.create_task(live_feed_loop(interval_seconds=1))
    await asyncio.sleep(3.5)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    async with AsyncSessionLocal() as db:
        after = (await db.execute(
            select(func.count()).select_from(SensorReading)
            .where(SensorReading.machine_id.in_(machine_ids))
        )).scalar_one()
        simulated = (await db.execute(
            select(func.count()).select_from(SensorReading)
            .where(SensorReading.machine_id.in_(machine_ids))
            .where(SensorReading.source == "simulator")
        )).scalar_one()

    assert after == before, "the simulator extended externally supplied data"
    assert simulated == 0, "a simulated reading was written to an external machine"


@pytest.mark.asyncio
async def test_uploaded_machines_are_marked_external(client, dataset):
    await client.post(
        f"/api/datasets/{dataset['id']}/upload",
        content=CSV.encode(), headers={"Content-Type": "text/csv"},
    )
    async with AsyncSessionLocal() as db:
        machines = (await db.execute(
            select(Machine).where(Machine.dataset_id == dataset["id"])
        )).scalars().all()
    assert machines
    for machine in machines:
        assert machine.data_origin == ORIGIN_EXTERNAL


@pytest.mark.asyncio
async def test_demo_fleet_remains_simulated_and_default_scoped(client):
    """The default machine list must keep showing exactly the demo fleet."""
    async with AsyncSessionLocal() as db:
        demo = (await db.execute(
            select(Machine).where(Machine.dataset_id.is_(None))
        )).scalars().all()
    assert demo
    for machine in demo:
        assert machine.data_origin == ORIGIN_SIMULATED

    listed = (await client.get("/api/machines")).json()
    assert len(listed) == len(demo)


@pytest.mark.asyncio
async def test_deleting_a_dataset_leaves_the_demo_fleet_intact(client):
    async with AsyncSessionLocal() as db:
        before = (await db.execute(
            select(func.count()).select_from(Machine).where(Machine.dataset_id.is_(None))
        )).scalar_one()

    created = (await client.post("/api/datasets", json={"name": "Throwaway"})).json()
    await client.post(
        f"/api/datasets/{created['id']}/upload",
        content=CSV.encode(), headers={"Content-Type": "text/csv"},
    )

    async with AsyncSessionLocal() as db:
        machine_ids = [
            row[0] for row in (await db.execute(
                select(Machine.id).where(Machine.dataset_id == created["id"])
            )).all()
        ]

    removed = (await client.delete(f"/api/datasets/{created['id']}")).json()
    assert removed["machines_deleted"] == 2
    assert removed["readings_deleted"] == 6

    async with AsyncSessionLocal() as db:
        after = (await db.execute(
            select(func.count()).select_from(Machine).where(Machine.dataset_id.is_(None))
        )).scalar_one()
        # Scoped to the deleted dataset's own rows. A global reading count would be
        # flaky here: the simulator legitimately keeps extending the demo fleet.
        orphans = (await db.execute(
            select(func.count()).select_from(SensorReading)
            .where(SensorReading.machine_id.in_(machine_ids))
        )).scalar_one()

    assert after == before, "the demo fleet lost machines"
    assert orphans == 0, "readings survived their deleted machine"


# ─── Baselines ────────────────────────────────────────────────────────────────

def test_baseline_uses_early_window_so_degradation_is_not_masked():
    from app.services.baseline_service import derive_baselines

    degrading = [
        {"vibration": 2.0 + i * 0.03, "temperature": 65 + i * 0.25, "rpm": 1450 - i}
        for i in range(200)
    ]
    derived = derive_baselines(degrading)
    naive_mean = sum(r["vibration"] for r in degrading) / len(degrading)
    assert derived["baseline_vibration"] < naive_mean
    assert derived["trusted"] is True


def test_baseline_resists_outliers():
    from app.services.baseline_service import derive_baselines

    readings = [{"vibration": 2.0, "temperature": 65, "rpm": 1450} for _ in range(60)]
    readings[30] = {"vibration": 500.0, "temperature": 900.0, "rpm": 0.0}
    derived = derive_baselines(readings)
    assert 1.9 <= derived["baseline_vibration"] <= 2.1


def test_short_history_baseline_is_flagged_untrusted():
    from app.services.baseline_service import derive_baselines

    derived = derive_baselines([{"vibration": 2.0, "temperature": 65, "rpm": 1450}] * 8)
    assert derived["trusted"] is False
    assert "low_confidence" in derived["baseline_method"]
    assert derive_baselines([])["baseline_method"] == "unavailable_no_readings"


def test_reference_precedence_is_design_spec_then_baseline():
    from app.services.baseline_service import reference_for

    class M:
        has_design_spec = 1
        nominal_vibration, nominal_temperature, nominal_rpm = 2.0, 65.0, 1450.0
        baseline_vibration = baseline_temperature = baseline_rpm = None

    machine = M()
    assert reference_for(machine)["source"] == "design_spec"

    machine.has_design_spec = 0
    assert reference_for(machine)["source"] == "assumed_default"

    machine.baseline_vibration, machine.baseline_temperature, machine.baseline_rpm = 3.1, 71.0, 900.0
    resolved = reference_for(machine)
    assert resolved["source"] == "derived_baseline"
    assert resolved["vibration"] == 3.1


@pytest.mark.asyncio
async def test_demo_fleet_baselines_recover_the_known_design_spec():
    """Independent check on the derivation: run against machines whose true spec we
    know, it should recover it. That is what justifies trusting it where we do not."""
    async with AsyncSessionLocal() as db:
        machines = (await db.execute(
            select(Machine).where(Machine.dataset_id.is_(None))
            .where(Machine.baseline_vibration.isnot(None))
            .limit(5)
        )).scalars().all()

    assert machines, "expected demo machines to have derived baselines"
    for machine in machines:
        assert abs(machine.baseline_vibration - machine.nominal_vibration) < 0.5, machine.name


# ─── Absolute limits: the baseline blind spot ─────────────────────────────────

def test_absolute_limits_catch_a_machine_that_was_always_bad():
    """If every supplied reading is degraded, the derived baseline treats the fault
    as normal and relative deviation reads zero. Published limits still fire."""
    from app.services.anomaly_service import absolute_concerns, absolute_score, deviation_score

    always_hot = {"vibration": 2.8, "temperature": 115.0, "rpm": 935.0}
    # Its own baseline, derived from uniformly hot data.
    self_reference = {"vibration": 2.8, "temperature": 114.0, "rpm": 935.0}

    assert deviation_score(always_hot, self_reference)["score"] == 0.0

    concerns = absolute_concerns(always_hot)
    assert concerns, "absolute limits must flag 115 C regardless of baseline"
    assert any(c["sensor"] == "temperature" and c["severity"] == "severe" for c in concerns)
    assert absolute_score(concerns) == 100.0


def test_absolute_limits_follow_published_thresholds():
    from app.services.anomaly_service import ISO_10816_ZONE_C, ISO_10816_ZONE_D, absolute_concerns

    healthy = absolute_concerns({"vibration": 1.2, "temperature": 60.0, "rpm": 1450})
    assert healthy == []

    unsatisfactory = absolute_concerns({"vibration": ISO_10816_ZONE_C + 0.1, "temperature": 60.0, "rpm": 1450})
    assert unsatisfactory[0]["severity"] == "concern"

    unacceptable = absolute_concerns({"vibration": ISO_10816_ZONE_D + 0.1, "temperature": 60.0, "rpm": 1450})
    assert unacceptable[0]["severity"] == "severe"
    assert "ISO 10816" in unacceptable[0]["basis"]


@pytest.mark.asyncio
async def test_always_degraded_upload_is_not_reported_as_normal(client, dataset):
    """End to end: a machine hot for its entire history must not read Normal."""
    hot = "machine,timestamp,vibration,temperature,rpm\n" + "".join(
        f"HOT-1,2026-03-01 {8 + i:02d}:00,2.8,{110 + i}.0,935\n" for i in range(6)
    )
    await client.post(
        f"/api/datasets/{dataset['id']}/upload",
        content=hot.encode(), headers={"Content-Type": "text/csv"},
    )
    report = (await client.get("/api/report", params={"dataset_id": dataset["id"]})).json()
    machine = report["machines"][0]

    assert machine["severity"] != "Normal", (
        "a machine at 110-115 C was reported as Normal"
    )
    assert machine["absolute_concerns"], "published limits should have flagged it"
    assert report["headline"]["machines_over_absolute_limits"] >= 1


# ─── Data quality ─────────────────────────────────────────────────────────────

def _quality_rows(count, vibration=2.0, flat_vibration=False, gap_at=None, duplicate=False, absurd=False):
    base = datetime(2026, 1, 1, 8, 0)
    rows = []
    stamp = base
    for i in range(count):
        if gap_at is not None and i == gap_at:
            stamp += timedelta(minutes=600)
        rows.append({
            "machine": "M",
            "timestamp": stamp,
            "vibration": vibration if flat_vibration else vibration + (i % 5) * 0.01,
            "temperature": 900.0 if absurd and i == 2 else 65.0 + (i % 3) * 0.2,
            "rpm": 1450.0 + (i % 7),
        })
        if duplicate and i == 3:
            rows.append(dict(rows[-1]))
        stamp += timedelta(minutes=60)
    return rows


def test_quality_accepts_clean_data():
    from app.services.data_quality import assess_quality

    report = assess_quality(_quality_rows(60))
    assert report["overall"] == "ok"
    assert report["machines"][0]["cadence_minutes"] == 60.0


def test_quality_flags_flatlined_sensor_as_unusable():
    """A dead transducer reads perfectly steady, which looks like a healthy machine."""
    from app.services.data_quality import assess_quality

    report = assess_quality(_quality_rows(40, flat_vibration=True))
    assert report["overall"] == "unusable"
    assert any(i["check"] == "flatlined_sensor" and i["severity"] == "error" for i in report["issues"])


def test_quality_treats_constant_rpm_as_only_a_warning():
    """A fixed-setpoint drive legitimately reports constant RPM, so this must not
    disqualify an otherwise good upload."""
    from app.services.data_quality import assess_quality

    rows = _quality_rows(40)
    for row in rows:
        row["rpm"] = 1450.0
    report = assess_quality(rows)
    assert report["overall"] == "warnings"
    assert all(
        i["severity"] == "warning"
        for i in report["issues"] if i["check"] == "flatlined_sensor"
    )


def test_quality_detects_gaps_duplicates_and_absurd_values():
    from app.services.data_quality import assess_quality

    gappy = assess_quality(_quality_rows(40, gap_at=15, duplicate=True))
    checks = {i["check"] for i in gappy["issues"]}
    assert "gaps" in checks
    assert "duplicate_timestamps" in checks

    absurd = assess_quality(_quality_rows(40, absurd=True))
    assert absurd["overall"] == "unusable"
    assert any(i["check"] == "implausible_values" for i in absurd["issues"])


def test_quality_warns_on_insufficient_history():
    from app.services.data_quality import assess_quality
    from app.services.data_quality import MIN_READINGS_FOR_FULL_FEATURES

    report = assess_quality(_quality_rows(8))
    assert report["overall"] == "warnings"
    detail = next(i["detail"] for i in report["issues"] if i["check"] == "insufficient_history")
    assert str(MIN_READINGS_FOR_FULL_FEATURES) in detail


def test_quality_rejects_empty_input():
    from app.services.data_quality import assess_quality

    assert assess_quality([])["overall"] == "unusable"


@pytest.mark.asyncio
async def test_quality_checks_also_run_against_the_demo_fleet(client):
    """A check that only ever runs on uploads is unverifiable. Passing it on the
    built-in data is what demonstrates it works."""
    report = (await client.get("/api/datasets/0/quality")).json()
    assert report["overall"] in ("ok", "warnings")
    assert report["summary"]["machines"] > 0
    assert report["summary"]["error_count"] == 0


# ─── Report ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_report_shape_is_identical_for_demo_and_upload(client, dataset):
    await client.post(
        f"/api/datasets/{dataset['id']}/upload",
        content=CSV.encode(), headers={"Content-Type": "text/csv"},
    )
    demo = (await client.get("/api/report")).json()
    uploaded = (await client.get("/api/report", params={"dataset_id": dataset["id"]})).json()

    assert sorted(demo.keys()) == sorted(uploaded.keys())
    assert sorted(demo["machines"][0].keys()) == sorted(uploaded["machines"][0].keys())
    assert demo["scope"]["contains_external_data"] is False
    assert uploaded["scope"]["contains_external_data"] is True


@pytest.mark.asyncio
async def test_report_always_states_its_limitations(client):
    report = (await client.get("/api/report")).json()
    joined = " ".join(report["limitations"]).lower()
    assert "synthetic" in joined
    assert "modelled" in joined
    assert len(report["limitations"]) >= 4


@pytest.mark.asyncio
async def test_report_ranks_on_the_worst_signal(client):
    """A machine only one method flags must not be buried at the bottom."""
    report = (await client.get("/api/report")).json()
    scores = [
        max(m["ml_risk_score"] or 0.0, m["deviation_score"] or 0.0, m["absolute_score"] or 0.0)
        for m in report["machines"]
    ]
    assert scores == sorted(scores, reverse=True)


@pytest.mark.asyncio
async def test_report_reports_both_signals_without_an_alert(client, dataset):
    """Signals are computed in the report, so a machine with no open alert still
    shows them; otherwise the report could not be used to audit the flagging."""
    await client.post(
        f"/api/datasets/{dataset['id']}/upload",
        content=CSV.encode(), headers={"Content-Type": "text/csv"},
    )
    async with AsyncSessionLocal() as db:
        machine_ids = [
            row[0] for row in (await db.execute(
                select(Machine.id).where(Machine.dataset_id == dataset["id"])
            )).all()
        ]
        await db.execute(delete(Alert).where(Alert.machine_id.in_(machine_ids)))
        await db.commit()

    report = (await client.get("/api/report", params={"dataset_id": dataset["id"]})).json()
    for machine in report["machines"]:
        assert machine["deviation_score"] is not None
        assert machine["confidence"] is not None
        assert machine["reference_source"]


@pytest.mark.asyncio
async def test_html_report_is_self_contained(client):
    response = await client.get("/api/report/html")
    assert response.status_code == 200
    body = response.text
    # No remote assets: it has to open offline, as a keepable artefact.
    assert "http://" not in body
    assert "https://" not in body
    assert "Basis and limitations" in body


@pytest.mark.asyncio
async def test_report_rejects_an_unknown_scope(client):
    assert (await client.get("/api/report", params={"dataset_id": 999999})).status_code == 404
