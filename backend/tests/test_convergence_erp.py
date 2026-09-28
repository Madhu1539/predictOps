"""
Tests for IT/OT convergence: ERP-driven production impact, automatic work orders,
and the machine ERP context endpoint.

These cover the parts of the problem statement that were previously plumbed but
inert — ERP data existed and influenced nothing, and work orders were
auto-populated but never auto-raised.
"""
from datetime import datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.config import Settings
from app.database import AsyncSessionLocal
from app.main import app
from app.models.alert import Alert
from app.models.machine import Machine
from app.models.production_order import ProductionOrder
from app.models.work_order import WorkOrder
from app.services.priority_service import (
    IMPACT_HORIZON_DAYS,
    calculate_maintenance_priority,
    classify_committed_quantity,
    get_production_impact,
    resolve_production_impact,
)
from app.services.workorder_service import (
    auto_create_work_order,
    get_due_date,
    get_work_order_priority,
    has_open_work_order,
)


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _any_machine(db):
    return (await db.execute(select(Machine).limit(1))).scalar_one_or_none()


# ─── Production impact from the ERP schedule ──────────────────────────────────

def test_committed_quantity_classification_is_monotonic():
    """More committed output must never lower the impact label."""
    labels = [classify_committed_quantity(q) for q in (0, 100, 500, 1500, 5000)]
    rank = {"Low": 0, "Medium": 1, "High": 2}
    assert [rank[l] for l in labels] == sorted(rank[l] for l in labels)


def test_high_committed_quantity_is_high_impact():
    assert classify_committed_quantity(2000) == "High"
    assert classify_committed_quantity(600) == "Medium"
    assert classify_committed_quantity(10) == "Low"


async def test_impact_falls_back_when_no_orders_exist():
    """A machine with no ERP orders must still get an impact, and must say so."""
    async with AsyncSessionLocal() as db:
        machine = Machine(
            name=f"TEST-NOORDERS-{datetime.utcnow().timestamp()}",
            type="Industrial Pump", location="Test", install_date="2024-01-01",
            criticality="High", nominal_vibration=2.0, nominal_temperature=65.0,
            nominal_rpm=1450.0,
        )
        db.add(machine)
        await db.flush()

        result = await resolve_production_impact(db, machine.id, "High")
        assert result["basis"] == "criticality_fallback"
        assert result["impact"] == "High"        # from criticality
        assert result["orders"] == []
        assert result["committed_units"] == 0
        assert "No open ERP production order" in result["detail"]

        await db.rollback()


async def test_impact_reads_real_orders_and_reports_remaining_quantity():
    """Impact must come from what is still to be produced, not the planned size:
    a nearly-finished large order is a smaller exposure."""
    async with AsyncSessionLocal() as db:
        machine = Machine(
            name=f"TEST-ORDERS-{datetime.utcnow().timestamp()}",
            type="CNC Machine", location="Test", install_date="2024-01-01",
            # Deliberately Low, so a High result can only have come from the order.
            criticality="Low", nominal_vibration=2.0, nominal_temperature=65.0,
            nominal_rpm=1450.0,
        )
        db.add(machine)
        await db.flush()

        db.add(ProductionOrder(
            order_no=f"TEST-PO-{datetime.utcnow().timestamp()}",
            machine_id=machine.id, product="Widget",
            planned_qty=2000, actual_qty=200,       # 1800 remaining -> High
            scheduled_start=datetime.utcnow() + timedelta(days=1),
            status="Released",
        ))
        await db.flush()

        result = await resolve_production_impact(db, machine.id, "Low")
        assert result["basis"] == "erp_production_orders"
        assert result["committed_units"] == 1800
        assert result["impact"] == "High"
        assert len(result["orders"]) == 1
        assert result["orders"][0]["remaining_qty"] == 1800

        await db.rollback()


async def test_orders_beyond_the_horizon_are_ignored():
    """An order starting after the prediction window is not what this alert is about."""
    async with AsyncSessionLocal() as db:
        machine = Machine(
            name=f"TEST-FARORDER-{datetime.utcnow().timestamp()}",
            type="Conveyor Motor", location="Test", install_date="2024-01-01",
            criticality="Low", nominal_vibration=2.0, nominal_temperature=65.0,
            nominal_rpm=1450.0,
        )
        db.add(machine)
        await db.flush()

        db.add(ProductionOrder(
            order_no=f"TEST-PO-FAR-{datetime.utcnow().timestamp()}",
            machine_id=machine.id, product="Widget",
            planned_qty=5000, actual_qty=0,
            scheduled_start=datetime.utcnow() + timedelta(days=IMPACT_HORIZON_DAYS + 10),
            status="Released",
        ))
        await db.flush()

        result = await resolve_production_impact(db, machine.id, "Low")
        assert result["basis"] == "criticality_fallback"
        assert result["committed_units"] == 0

        await db.rollback()


async def test_completed_orders_do_not_count_as_exposure():
    """Output already banked cannot be lost to a future failure."""
    async with AsyncSessionLocal() as db:
        machine = Machine(
            name=f"TEST-DONEORDER-{datetime.utcnow().timestamp()}",
            type="Hydraulic Press", location="Test", install_date="2024-01-01",
            criticality="Low", nominal_vibration=2.0, nominal_temperature=65.0,
            nominal_rpm=1450.0,
        )
        db.add(machine)
        await db.flush()

        db.add(ProductionOrder(
            order_no=f"TEST-PO-DONE-{datetime.utcnow().timestamp()}",
            machine_id=machine.id, product="Widget",
            planned_qty=5000, actual_qty=5000,
            scheduled_start=datetime.utcnow(),
            status="Complete",
        ))
        await db.flush()

        result = await resolve_production_impact(db, machine.id, "Low")
        assert result["basis"] == "criticality_fallback"

        await db.rollback()


def test_priority_no_longer_double_counts_criticality():
    """The old `get_production_impact` derived impact FROM criticality, so
    priority multiplied criticality by a proxy for itself. Confirm the fallback
    still behaves that way, which is exactly why ERP data is preferred."""
    assert get_production_impact("High") == "High"
    assert get_production_impact("Low") == "Low"

    # Same risk and criticality, impact supplied independently -> different priority.
    from_erp_high = calculate_maintenance_priority(80.0, "Low", "High")
    from_erp_low = calculate_maintenance_priority(80.0, "Low", "Low")
    assert from_erp_high > from_erp_low


# ─── Automatic work orders ────────────────────────────────────────────────────

def test_work_order_due_date_and_priority_track_severity():
    now = datetime.utcnow()
    assert (get_due_date("Critical") - now) < timedelta(days=2)
    assert timedelta(days=6) < (get_due_date("Warning") - now) < timedelta(days=8)
    assert get_work_order_priority("Critical") == "Urgent"
    assert get_work_order_priority("Watch") == "Low"


async def test_auto_work_order_is_skipped_when_disabled(monkeypatch):
    import app.services.workorder_service as svc

    monkeypatch.setattr(
        svc, "get_settings",
        lambda: Settings(auto_work_order_enabled=False, environment="test"),
    )
    async with AsyncSessionLocal() as db:
        machine = await _any_machine(db)
        alert = Alert(machine_id=machine.id, risk_score=90.0, severity="Critical",
                      maintenance_priority=90.0, status="Active")
        db.add(alert)
        await db.flush()
        assert await svc.auto_create_work_order(db, alert, machine.name) is None
        await db.rollback()


async def test_auto_work_order_ignores_non_qualifying_severity(monkeypatch):
    """Warning is excluded by default: at ~0.53 precision, auto-raising every
    Warning would bury planners in work orders that resolve on their own."""
    import app.services.workorder_service as svc

    monkeypatch.setattr(
        svc, "get_settings",
        lambda: Settings(auto_work_order_enabled=True,
                         auto_work_order_severities="Critical", environment="test"),
    )
    async with AsyncSessionLocal() as db:
        machine = await _any_machine(db)
        alert = Alert(machine_id=machine.id, risk_score=55.0, severity="Warning",
                      maintenance_priority=55.0, status="Active")
        db.add(alert)
        await db.flush()
        assert await svc.auto_create_work_order(db, alert, machine.name) is None
        await db.rollback()


async def test_auto_work_order_is_duplicate_guarded(monkeypatch):
    """The live feed re-scores every machine on each tick. Without this guard a
    machine that stays Critical accumulates a work order per tick."""
    import app.services.workorder_service as svc

    monkeypatch.setattr(
        svc, "get_settings",
        lambda: Settings(auto_work_order_enabled=True,
                         auto_work_order_severities="Critical", environment="test"),
    )
    async with AsyncSessionLocal() as db:
        machine = Machine(
            name=f"TEST-AUTOWO-{datetime.utcnow().timestamp()}",
            type="Industrial Pump", location="Test", install_date="2024-01-01",
            criticality="High", nominal_vibration=2.0, nominal_temperature=65.0,
            nominal_rpm=1450.0,
        )
        db.add(machine)
        await db.flush()

        assert await has_open_work_order(db, machine.id) is False

        alert = Alert(machine_id=machine.id, risk_score=95.0, severity="Critical",
                      maintenance_priority=95.0, status="Active",
                      part_needed="Bearing Set")
        db.add(alert)
        await db.flush()

        first = await svc.auto_create_work_order(db, alert, machine.name)
        assert first is not None
        assert first.priority == "Urgent"
        assert first.status == "Open"
        assert await has_open_work_order(db, machine.id) is True

        # Second attempt for the same still-Critical machine must be a no-op.
        second = await svc.auto_create_work_order(db, alert, machine.name)
        assert second is None

        created = (
            await db.execute(select(WorkOrder).where(WorkOrder.machine_id == machine.id))
        ).scalars().all()
        assert len(created) == 1

        # Clean up: auto_create_work_order commits, so rollback will not undo it.
        for wo in created:
            await db.delete(wo)
        await db.delete(alert)
        await db.delete(machine)
        await db.commit()


# ─── ERP machine context endpoint ─────────────────────────────────────────────

async def test_machine_erp_context_returns_convergence_payload(client):
    async with AsyncSessionLocal() as db:
        machine = await _any_machine(db)
        machine_id = machine.id

    response = await client.get(f"/api/erp/machine-context/{machine_id}")
    assert response.status_code == 200
    body = response.json()

    assert body["machine_id"] == machine_id
    assert body["production_impact"] in ("Low", "Medium", "High")
    assert body["production_impact_basis"] in (
        "erp_production_orders", "criticality_fallback",
    )
    # The basis is always stated, so a fallback is never mistaken for ERP evidence.
    assert body["production_impact_detail"]
    assert isinstance(body["open_orders"], list)
    assert "impact_horizon_days" in body["assumptions"]


async def test_machine_erp_context_404s_for_unknown_machine(client):
    response = await client.get("/api/erp/machine-context/99999999")
    assert response.status_code == 404
    assert response.json()["code"] == "MACHINE_NOT_FOUND"


# ─── Investigation evidence contract ──────────────────────────────────────────

async def test_healthy_machine_still_carries_evidence():
    """A "not currently flagged" answer quotes live sensor values against nominals,
    so those readings must appear in the evidence array. They previously did not,
    because the builder only ever returned model attribution."""
    from app.llm.investigate_service import _evidence_rows

    rows = _evidence_rows("why_at_risk", {
        "top_factors": [],
        "risk_score": 0.0,
        "has_open_alert": False,
        "deviation_score": 3.2,
        "current": {"vibration": 1.7, "temperature": 63.3, "rpm": 1488.4},
        "nominal": {"vibration": 2.0, "temperature": 65.0, "rpm": 1450.0},
    })
    assert rows, "a healthy machine must still produce checkable evidence"
    sensors = {r.get("sensor") for r in rows if "sensor" in r}
    assert sensors == {"vibration", "temperature", "rpm"}
    signals = {r.get("signal") for r in rows if "signal" in r}
    assert "ml_risk" in signals and "deviation" in signals


def test_flagged_machine_prefers_model_attribution():
    """When the model did run, its own contributions remain the evidence."""
    from app.llm.investigate_service import _evidence_rows

    factors = [{"feature": "temperature_mean", "label": "Temperature", "value": 98.7,
                "contribution": 3.6, "direction": "increases_risk", "method": "x"}]
    rows = _evidence_rows("why_at_risk", {
        "top_factors": factors,
        "current": {"vibration": 9.9},
        "nominal": {"vibration": 2.0},
    })
    assert rows == factors
