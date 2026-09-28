"""
API contract tests (spec §52).

Exercises every endpoint in the §46 contract with both valid and invalid
requests, against the real application and database.
"""
import asyncio
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, delete, func

from app.database import init_db, AsyncSessionLocal
from app.main import app
from app.models.alert import Alert
from app.models.machine import Machine
from app.models.oee import OeeSnapshot
from app.models.work_order import WorkOrder


@pytest_asyncio.fixture
async def client():
    await init_db()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def seeded_alert():
    """A Critical alert on a real machine, cleaned up afterwards."""
    await init_db()
    async with AsyncSessionLocal() as db:
        machine = (await db.execute(select(Machine).limit(1))).scalar_one()
        alert = Alert(
            machine_id=machine.id,
            risk_score=87.0,
            severity="Critical",
            maintenance_priority=96.0,
            status="Active",
            part_needed="Bearing Assembly",
            part_available=True,
            failure_mode="BEARING_DEGRADATION",
            recommended_action="Inspect bearing assembly",
            days_since_maintenance=63.0,
            previous_failure_context="Previous Bearing Degradation recorded",
            machine_criticality=machine.criticality,
            production_impact="High",
        )
        db.add(alert)
        await db.commit()
        await db.refresh(alert)
        alert_id, machine_id = alert.id, machine.id

    yield {"alert_id": alert_id, "machine_id": machine_id}

    async with AsyncSessionLocal() as db:
        await db.execute(delete(WorkOrder).where(WorkOrder.alert_id == alert_id))
        await db.execute(delete(Alert).where(Alert.id == alert_id))
        await db.commit()


# ─── Health ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_health_returns_ok(client):
    r = await client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "demo_mode" in body
    assert "degraded_mode" in body


# ─── Machines ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_machines(client):
    r = await client.get("/api/machines")
    assert r.status_code == 200
    machines = r.json()
    assert len(machines) > 0
    for key in ("id", "name", "type", "criticality"):
        assert key in machines[0]


@pytest.mark.asyncio
async def test_get_machine_detail(client):
    machines = (await client.get("/api/machines")).json()
    r = await client.get(f"/api/machines/{machines[0]['id']}")
    assert r.status_code == 200
    body = r.json()
    for key in ("readings", "maintenance", "risk_history"):
        assert key in body


@pytest.mark.asyncio
async def test_get_machine_not_found(client):
    r = await client.get("/api/machines/999999")
    assert r.status_code == 404
    assert r.json()["code"] == "MACHINE_NOT_FOUND"


# ─── Alerts ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_alerts(client, seeded_alert):
    r = await client.get("/api/alerts")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


@pytest.mark.asyncio
async def test_alert_filters_are_applied_in_sql(client, seeded_alert):
    """A filtered query must only return matching rows (filter before LIMIT)."""
    r = await client.get("/api/alerts", params={"severity": "Critical"})
    assert r.status_code == 200
    assert all(a["severity"] == "Critical" for a in r.json())

    r = await client.get("/api/alerts", params={"status": "Active"})
    assert all(a["status"] == "Active" for a in r.json())


@pytest.mark.asyncio
async def test_get_alert_by_id(client, seeded_alert):
    r = await client.get(f"/api/alerts/{seeded_alert['alert_id']}")
    assert r.status_code == 200
    assert r.json()["risk_score"] == 87.0


@pytest.mark.asyncio
async def test_get_alert_not_found(client):
    r = await client.get("/api/alerts/999999")
    assert r.status_code == 404
    assert r.json()["code"] == "ALERT_NOT_FOUND"


@pytest.mark.asyncio
async def test_explain_alert_always_returns_text(client, seeded_alert):
    """Must succeed with or without Gemini — deterministic fallback (spec §31)."""
    r = await client.post(f"/api/alerts/{seeded_alert['alert_id']}/explain")
    assert r.status_code == 200
    body = r.json()
    assert len(body["explanation"]) > 40
    assert body["source"] in ("gemini", "deterministic", "cached")


@pytest.mark.asyncio
async def test_explain_alert_is_cached(client, seeded_alert):
    first = await client.post(f"/api/alerts/{seeded_alert['alert_id']}/explain")
    second = await client.post(f"/api/alerts/{seeded_alert['alert_id']}/explain")
    assert second.status_code == 200
    assert second.json()["explanation"] == first.json()["explanation"]


@pytest.mark.asyncio
async def test_patch_alert_status(client, seeded_alert):
    r = await client.patch(
        f"/api/alerts/{seeded_alert['alert_id']}", json={"status": "Acknowledged"}
    )
    assert r.status_code == 200
    assert r.json()["status"] == "Acknowledged"


@pytest.mark.asyncio
async def test_patch_alert_rejects_invalid_status(client, seeded_alert):
    r = await client.patch(
        f"/api/alerts/{seeded_alert['alert_id']}", json={"status": "Bogus"}
    )
    assert r.status_code == 400


# ─── Work Orders ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_work_order_from_alert(client, seeded_alert):
    r = await client.post(f"/api/alerts/{seeded_alert['alert_id']}/workorder")
    assert r.status_code in (200, 201)
    wo = r.json()
    # Critical severity maps to Urgent with a next-business-day due date (§36).
    assert wo["priority"] == "Urgent"
    assert wo["status"] == "Open"
    assert wo["part_needed"] == "Bearing Assembly"
    assert wo["technician"]


@pytest.mark.asyncio
async def test_work_order_priority_stays_in_enum(client, seeded_alert):
    r = await client.post(
        "/api/workorders",
        json={"alert_id": seeded_alert["alert_id"], "machine_id": seeded_alert["machine_id"]},
    )
    assert r.status_code == 201
    assert r.json()["priority"] in ("Urgent", "Medium", "Low")


@pytest.mark.asyncio
async def test_create_work_order_unknown_machine(client):
    r = await client.post("/api/workorders", json={"machine_id": 999999})
    assert r.status_code == 404
    assert r.json()["code"] == "MACHINE_NOT_FOUND"


@pytest.mark.asyncio
async def test_create_work_order_validation_error(client):
    r = await client.post("/api/workorders", json={"machine_id": "abc"})
    assert r.status_code == 400
    assert r.json()["code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_list_work_orders_filter(client):
    r = await client.get("/api/workorders", params={"status": "Open"})
    assert r.status_code == 200
    assert all(w["status"] == "Open" for w in r.json())


@pytest.mark.asyncio
async def test_work_order_rejects_illegal_transition(client, seeded_alert):
    created = (await client.post(f"/api/alerts/{seeded_alert['alert_id']}/workorder")).json()
    await client.patch(f"/api/workorders/{created['id']}", json={"status": "Completed"})
    # Completed is terminal.
    r = await client.patch(f"/api/workorders/{created['id']}", json={"status": "Open"})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_work_order_not_found(client):
    r = await client.patch("/api/workorders/999999", json={"status": "Completed"})
    assert r.status_code == 404


# ─── OEE ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_oee(client):
    r = await client.get("/api/oee")
    assert r.status_code == 200
    body = r.json()
    assert 0.0 <= body["plant_oee"] <= 1.0
    assert "trend" in body and "per_machine" in body


# ─── §53 Critical Integration Test ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_end_to_end_workflow(client, seeded_alert):
    """The full spec §53 chain: alert -> explanation -> recommendation ->
    work order -> completion -> alert resolution.

    The OEE recovery step is asserted separately in
    `test_completion_recovers_oee`, on a machine the live feed cannot touch, so
    this test stays deterministic while a background feed is running.
    """
    alert_id = seeded_alert["alert_id"]

    # Risk score and priority are present on the alert.
    alert = (await client.get(f"/api/alerts/{alert_id}")).json()
    assert alert["risk_score"] >= 75
    assert alert["severity"] == "Critical"
    assert alert["maintenance_priority"] > 0

    # Explanation is grounded and always available.
    explanation = (await client.post(f"/api/alerts/{alert_id}/explain")).json()
    assert explanation["explanation"]

    # Recommendation is present.
    assert alert["recommended_action"] == "Inspect bearing assembly"

    # Work order is created with a technician and urgent priority.
    wo = (await client.post(f"/api/alerts/{alert_id}/workorder")).json()
    assert wo["technician"] and wo["priority"] == "Urgent"
    assert wo["part_needed"] == "Bearing Assembly"

    # Progress Open -> InProgress -> Completed.
    assert (await client.patch(f"/api/workorders/{wo['id']}", json={"status": "InProgress"})).status_code == 200
    completed = await client.patch(f"/api/workorders/{wo['id']}", json={"status": "Completed"})
    assert completed.status_code == 200
    assert completed.json()["status"] == "Completed"

    # Completion resolves the originating alert.
    resolved = (await client.get(f"/api/alerts/{alert_id}")).json()
    assert resolved["status"] == "Resolved"

    # Plant OEE remains a valid 0-1 figure throughout.
    assert 0.0 <= (await client.get("/api/oee")).json()["plant_oee"] <= 1.0


@pytest.mark.asyncio
async def test_completion_recovers_oee(client):
    """Completing maintenance must raise the machine's simulated OEE (spec §38).

    Uses a dedicated machine with no sensor readings: the live feed skips such
    machines, so no background write can change the snapshot under the test.
    """
    async with AsyncSessionLocal() as db:
        machine = Machine(
            name="ZZ-TEST-OEE",
            type="CNC Machine",
            location="Test Cell",
            install_date=datetime(2020, 1, 1),
            criticality="High",
            nominal_vibration=2.0,
            nominal_temperature=65.0,
            nominal_rpm=1450.0,
        )
        db.add(machine)
        await db.commit()
        await db.refresh(machine)
        machine_id = machine.id

        alert = Alert(
            machine_id=machine_id, risk_score=90.0, severity="Critical",
            maintenance_priority=100.0, status="Active",
        )
        db.add(alert)
        # Baseline with real downtime, so recovery has headroom.
        db.add(OeeSnapshot(
            machine_id=machine_id, timestamp=datetime.utcnow(),
            availability=0.80, performance=0.90, quality=0.95, oee=0.684,
        ))
        await db.commit()
        await db.refresh(alert)
        alert_id = alert.id

    try:
        wo = (await client.post(f"/api/alerts/{alert_id}/workorder")).json()
        await client.patch(f"/api/workorders/{wo['id']}", json={"status": "Completed"})

        async with AsyncSessionLocal() as db:
            latest = (await db.execute(
                select(OeeSnapshot)
                .where(OeeSnapshot.machine_id == machine_id)
                .order_by(OeeSnapshot.timestamp.desc()).limit(1)
            )).scalar_one()

        assert latest.availability > 0.80, "availability must recover"
        assert latest.oee > 0.684, "OEE must improve after maintenance"
        # Performance and quality carry over, so the gain is attributable to
        # recovered availability alone.
        assert latest.performance == pytest.approx(0.90)
        assert latest.quality == pytest.approx(0.95)
    finally:
        async with AsyncSessionLocal() as db:
            await db.execute(delete(WorkOrder).where(WorkOrder.machine_id == machine_id))
            await db.execute(delete(OeeSnapshot).where(OeeSnapshot.machine_id == machine_id))
            await db.execute(delete(Alert).where(Alert.machine_id == machine_id))
            await db.execute(delete(Machine).where(Machine.id == machine_id))
            await db.commit()
