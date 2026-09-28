"""
Tests for the IT/OT convergence, value model, explainability, investigation,
auth and transparency features.

Kept separate from `test_api.py` so the original §52 contract suite stays focused
on the spec's own endpoint list.
"""
import json
import os
from datetime import datetime, timedelta

import pandas as pd
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.database import AsyncSessionLocal, init_db
from app.main import app
from app.models.alert import Alert
from app.models.auth import AuditLog, NotificationLog, User
from app.models.cost_center import CostCenter
from app.models.machine import Machine
from app.models.material import Material
from app.models.production_order import ProductionOrder
from app.models.sensor import SensorReading
from app.services import cost_service


@pytest_asyncio.fixture
async def client():
    await init_db()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def ingest_machine():
    """A dedicated machine for ingestion tests, with its rows cleaned up after.

    Uses its own machine so ingesting degraded readings cannot disturb the demo
    machines other tests and the live feed rely on.
    """
    await init_db()
    async with AsyncSessionLocal() as db:
        machine = Machine(
            name="ZZ-INGEST-TEST",
            type="CNC Machine",
            location="Line A",
            install_date="2020-01-01",
            criticality="Medium",
            nominal_vibration=2.0,
            nominal_temperature=65.0,
            nominal_rpm=1450.0,
            cost_center_code="CC-100",
            ideal_units_per_hour=720.0,
        )
        db.add(machine)
        await db.commit()
        await db.refresh(machine)
        machine_id = machine.id

    yield machine_id

    async with AsyncSessionLocal() as db:
        await db.execute(delete(SensorReading).where(SensorReading.machine_id == machine_id))
        await db.execute(delete(Alert).where(Alert.machine_id == machine_id))
        await db.execute(delete(Machine).where(Machine.id == machine_id))
        await db.commit()


# ─── ERP layer ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cost_centers_seeded_and_exposed(client):
    response = await client.get("/api/erp/cost-centers")
    assert response.status_code == 200
    rows = response.json()
    assert len(rows) > 0
    assert all(r["downtime_cost_per_hour"] > 0 for r in rows)
    assert all(r["erp_source"] == "ERP" for r in rows)


@pytest.mark.asyncio
async def test_every_machine_is_linked_to_a_cost_centre(client):
    """An unlinked machine would silently fall back to a default cost, making its
    financial exposure wrong rather than obviously missing.

    Scoped to the seeded demo fleet by `dataset_id is None`. Machines created by a
    user upload carry no ERP cost centre — a CSV of sensor readings has no such
    column — so asserting over every row in the table made this test fail for
    uploaded data rather than for a broken ERP link.
    """
    codes = {c["code"] for c in (await client.get("/api/erp/cost-centers")).json()}
    async with AsyncSessionLocal() as db:
        machines = (await db.execute(select(Machine))).scalars().all()
    seeded = [
        m for m in machines
        if m.dataset_id is None and not m.name.startswith("ZZ-")
    ]
    assert seeded, "expected seeded machines"
    for machine in seeded:
        assert machine.cost_center_code in codes, machine.name


@pytest.mark.asyncio
async def test_materials_join_to_spare_part_stock(client):
    rows = (await client.get("/api/erp/materials")).json()
    assert len(rows) > 0
    # The name-based join must actually resolve, otherwise cost and stock appear
    # in separate silos.
    assert any(r["stock_quantity"] is not None for r in rows)
    assert all(r["unit_cost"] >= 0 and r["lead_time_days"] >= 0 for r in rows)


@pytest.mark.asyncio
async def test_production_orders_filter_in_sql(client):
    rows = (await client.get("/api/erp/production-orders", params={"status": "InProgress"})).json()
    assert rows, "expected in-progress orders"
    assert {r["status"] for r in rows} == {"InProgress"}
    assert all(r["machine_name"] for r in rows)


# ─── Ingestion ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ingest_batch_scores_machine(client, ingest_machine):
    payload = {
        "machine_id": ingest_machine,
        "readings": [
            {"vibration": 2.0, "temperature": 65.0, "rpm": 1450,
             "production_count": 600, "good_count": 590}
            for _ in range(3)
        ],
    }
    response = await client.post("/api/readings", json=payload)
    assert response.status_code == 201
    body = response.json()
    assert body["accepted"] == 3
    assert body["rejected"] == 0
    assert body["source"] == "api"
    assert body["risk_score"] is not None


@pytest.mark.asyncio
async def test_ingested_degraded_readings_raise_risk(client, ingest_machine):
    """Ingested data must drive the model, not merely be stored."""
    healthy = {
        "machine_id": ingest_machine,
        "readings": [{"vibration": 2.0, "temperature": 65.0, "rpm": 1450}] * 3,
    }
    baseline = (await client.post("/api/readings", json=healthy)).json()["risk_score"]

    degraded = {
        "machine_id": ingest_machine,
        "readings": [{"vibration": 5.2, "temperature": 95.0, "rpm": 1200}] * 6,
    }
    after = (await client.post("/api/readings", json=degraded)).json()["risk_score"]

    assert after > baseline


@pytest.mark.asyncio
async def test_ingest_rejects_unknown_machine(client):
    response = await client.post(
        "/api/readings",
        json={"machine_name": "NO-SUCH-MACHINE",
              "readings": [{"vibration": 1, "temperature": 20, "rpm": 100}]},
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_ingest_requires_a_machine(client):
    response = await client.post(
        "/api/readings",
        json={"readings": [{"vibration": 1, "temperature": 20, "rpm": 100}]},
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_ingest_rejects_invalid_values(client, ingest_machine):
    response = await client.post(
        "/api/readings",
        json={"machine_id": ingest_machine,
              "readings": [{"vibration": -5, "temperature": 65, "rpm": 1450}]},
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_csv_ingest_reports_bad_rows_individually(client, ingest_machine):
    """One malformed line must not discard the whole upload."""
    async with AsyncSessionLocal() as db:
        machine = (await db.execute(
            select(Machine).where(Machine.id == ingest_machine)
        )).scalar_one()

    csv_text = (
        "timestamp,vibration,temperature,rpm\n"
        "2026-01-01T10:00:00,2.0,65,1450\n"
        "2026-01-01T11:00:00,NOT_A_NUMBER,65,1450\n"
        "2026-01-01T12:00:00,2.1,66,1445\n"
    )
    response = await client.post(
        "/api/readings/csv",
        params={"machine_name": machine.name},
        content=csv_text.encode(),
        headers={"Content-Type": "text/csv"},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["accepted"] == 2
    assert body["rejected"] == 1
    assert body["errors"][0]["row"] == 3   # header is line 1


@pytest.mark.asyncio
async def test_csv_ingest_requires_sensor_columns(client, ingest_machine):
    async with AsyncSessionLocal() as db:
        machine = (await db.execute(
            select(Machine).where(Machine.id == ingest_machine)
        )).scalar_one()

    response = await client.post(
        "/api/readings/csv",
        params={"machine_name": machine.name},
        content=b"foo,bar\n1,2\n",
        headers={"Content-Type": "text/csv"},
    )
    assert response.status_code == 400
    assert "CSV_MISSING_COLUMNS" in response.json()["detail"]


@pytest.mark.asyncio
async def test_ingest_status_reports_provenance(client):
    body = (await client.get("/api/readings/status")).json()
    assert body["total_readings"] > 0
    sources = {s["source"] for s in body["by_source"]}
    assert "simulator" in sources


# ─── Cost model ───────────────────────────────────────────────────────────────

class _CostCentre:
    downtime_cost_per_hour = 4800.0
    currency = "USD"


class _Material:
    unit_cost = 480.0


def test_unplanned_costs_more_than_planned():
    """The whole premise of predictive maintenance: acting early is cheaper."""
    unplanned = cost_service.estimate_unplanned_failure_cost("BEARING_DEGRADATION", _CostCentre())
    planned = cost_service.estimate_planned_intervention_cost(
        "BEARING_DEGRADATION", _CostCentre(), _Material()
    )
    assert unplanned > planned


def test_loss_avoided_scales_with_risk():
    low = cost_service.estimate_loss_avoided(20, "BEARING_DEGRADATION", _CostCentre(), _Material())
    high = cost_service.estimate_loss_avoided(95, "BEARING_DEGRADATION", _CostCentre(), _Material())
    assert high > low


def test_loss_avoided_never_negative():
    """At low probability, waiting is rational; a negative "saving" would be noise."""
    assert cost_service.estimate_loss_avoided(0, "BEARING_DEGRADATION", _CostCentre(), _Material()) == 0.0


def test_missing_cost_centre_falls_back_not_free():
    """A machine with no ERP link must not appear to cost nothing when it stops."""
    cost = cost_service.estimate_unplanned_failure_cost("BEARING_DEGRADATION", None)
    assert cost > 0


def test_downtime_cost_is_proportional_to_time():
    one_hour = cost_service.estimate_downtime_cost(60, _CostCentre())
    two_hours = cost_service.estimate_downtime_cost(120, _CostCentre())
    assert round(two_hours, 2) == round(one_hour * 2, 2)
    assert cost_service.estimate_downtime_cost(0, _CostCentre()) == 0.0


def test_expensive_cost_centre_yields_higher_exposure():
    class Cheap:
        downtime_cost_per_hour = 1400.0
        currency = "USD"

    dear = cost_service.estimate_unplanned_failure_cost("OVERHEATING", _CostCentre())
    cheap = cost_service.estimate_unplanned_failure_cost("OVERHEATING", Cheap())
    assert dear > cheap


@pytest.mark.asyncio
async def test_impact_endpoint_publishes_its_assumptions(client):
    body = (await client.get("/api/impact")).json()
    assert body["value_at_risk"] >= 0
    assert "modelled" in body["basis"].lower()
    assert body["assumptions"]["labour_rate_per_hour"] > 0
    for exposure in body["top_exposure"]:
        assert exposure["downtime_cost_per_hour"] > 0


# ─── OEE ──────────────────────────────────────────────────────────────────────

def test_oee_uses_per_machine_ideal_rate():
    from app.services.oee_service import calculate_oee

    slow = calculate_oee(60, 60, 400, 400, ideal_units_per_hour=400)
    fast = calculate_oee(60, 60, 400, 400, ideal_units_per_hour=800)
    # At the same output, a machine designed to run faster has lost more
    # performance than one already at its design rate.
    assert slow["performance"] > fast["performance"]


def test_oee_defaults_when_rate_is_missing():
    from app.services.oee_service import DEFAULT_IDEAL_UNITS_PER_HOUR, calculate_oee

    explicit = calculate_oee(60, 60, 300, 300, ideal_units_per_hour=DEFAULT_IDEAL_UNITS_PER_HOUR)
    implicit = calculate_oee(60, 60, 300, 300)
    assert explicit == implicit


@pytest.mark.asyncio
async def test_oee_loss_decomposition_sums_to_one(client):
    """Losses plus achieved OEE must account for exactly the whole of ideal
    output, otherwise the same lost production is counted twice."""
    body = (await client.get("/api/oee/losses")).json()
    assert body["machines"], "expected OEE snapshots"
    for item in body["machines"]:
        total = (
            item["oee"]
            + item["availability_loss"]
            + item["performance_loss"]
            + item["quality_loss"]
        )
        assert abs(total - 1.0) < 0.01


@pytest.mark.asyncio
async def test_oee_losses_ranked_worst_first(client):
    body = (await client.get("/api/oee/losses")).json()
    oees = [m["oee"] for m in body["machines"]]
    assert oees == sorted(oees)


@pytest.mark.asyncio
async def test_oee_losses_rejects_impossible_target(client):
    assert (await client.get("/api/oee/losses", params={"target_oee": 5})).status_code == 400


# ─── Attribution ──────────────────────────────────────────────────────────────

def _feature_row():
    from app.ml.feature_engineering import FEATURE_COLUMNS

    return pd.DataFrame([{column: 1.0 for column in FEATURE_COLUMNS}])


def test_attribution_returns_ranked_contributions():
    from app.ml.attribution import attribute_prediction

    records = attribute_prediction(_feature_row(), top_n=5)
    assert records, "expected attribution from the trained model"
    assert len(records) <= 5
    magnitudes = [abs(r["contribution"]) for r in records]
    assert magnitudes == sorted(magnitudes, reverse=True)
    for record in records:
        assert record["direction"] in ("increases_risk", "reduces_risk")
        assert record["label"]


def test_attribution_handles_pipeline_without_scaler():
    """Random Forest has no scaler step, so attribution must probe rather than
    assume `named_steps['scaler']` exists."""
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.pipeline import Pipeline

    from app.ml.attribution import attribute_prediction
    from app.ml.feature_engineering import FEATURE_COLUMNS

    X = pd.DataFrame([[float(i)] * len(FEATURE_COLUMNS) for i in range(1, 11)],
                     columns=FEATURE_COLUMNS)
    y = [0, 1] * 5
    model = Pipeline([("clf", RandomForestClassifier(n_estimators=5, random_state=0))])
    model.fit(X, y)

    records = attribute_prediction(_feature_row(), model=model)
    assert records
    assert records[0]["method"] == "tree_importance_approx"


def test_attribution_unwraps_calibrated_classifier():
    """A calibration wrapper hides `coef_`, so the estimator must be unwrapped or
    explainability silently disappears whenever calibration is enabled."""
    from sklearn.calibration import CalibratedClassifierCV
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler

    from app.ml.attribution import attribute_prediction
    from app.ml.feature_engineering import FEATURE_COLUMNS

    X = pd.DataFrame([[float(i)] * len(FEATURE_COLUMNS) for i in range(1, 13)],
                     columns=FEATURE_COLUMNS)
    y = [0, 1] * 6
    model = Pipeline([
        ("scaler", StandardScaler()),
        ("clf", CalibratedClassifierCV(LogisticRegression(max_iter=200), cv=3)),
    ])
    model.fit(X, y)

    records = attribute_prediction(_feature_row(), model=model)
    assert records
    assert records[0]["method"] == "linear_coefficient"


def test_attribution_degrades_quietly_on_unusable_model():
    """Attribution is additive context; it must never break scoring."""
    from app.ml.attribution import attribute_prediction

    class Useless:
        pass

    assert attribute_prediction(_feature_row(), model=Useless()) == []


def test_top_sensors_ignores_risk_reducing_factors():
    from app.ml.attribution import top_sensors_from_attribution

    records = [
        {"feature": "temperature_mean", "contribution": 2.0},
        {"feature": "vibration_std", "contribution": 0.5},
        {"feature": "rpm_std", "contribution": -3.0},   # reduces risk
    ]
    sensors = top_sensors_from_attribution(records)
    assert sensors == ["temperature", "vibration"]
    assert "rpm" not in sensors


def test_predict_with_attribution_describes_the_scored_row():
    from app.ml.predict import predict_risk_with_attribution

    timestamps = pd.date_range(end=datetime.utcnow(), periods=30, freq="h")
    readings = pd.DataFrame({
        "machine_id": 1,
        "timestamp": timestamps,
        "vibration": 4.8,
        "temperature": 92.0,
        "rpm": 1300.0,
        "machine_status": "Running",
        "production_count": 500,
        "good_count": 450,
        "planned_production_time": 60.0,
        "actual_run_time": 55.0,
        "downtime_minutes": 5.0,
    })
    meta = {
        "type": "CNC Machine", "criticality": "High", "nominal_vibration": 2.0,
        "nominal_temperature": 65.0, "nominal_rpm": 1450.0, "install_date": "2018-01-01",
    }
    risk, records, exceedances = predict_risk_with_attribution(readings, meta, pd.DataFrame())
    assert 0 <= risk <= 100
    assert records
    # Exceedances are reported, not raised: a reading beyond the training range is
    # clamped so the score stays inside the model's evidence.
    assert isinstance(exceedances, list)


def test_global_importance_is_ranked():
    from app.ml.attribution import global_importance

    records = global_importance()
    assert records
    weights = [r["weight"] for r in records]
    assert weights == sorted(weights, reverse=True)


# ─── Maintenance context and inference guardrails ─────────────────────────────

def _noisy_readings(vibration, temperature, rpm, periods=40):
    import numpy as np

    rng = np.random.default_rng(7)
    timestamps = pd.date_range(end=datetime.utcnow(), periods=periods, freq="h")
    return pd.DataFrame({
        "machine_id": 1,
        "timestamp": timestamps,
        "vibration": vibration + rng.normal(0, 0.08, periods),
        "temperature": temperature + rng.normal(0, 1.2, periods),
        "rpm": rpm + rng.normal(0, 8, periods),
        "machine_status": "Running",
        "production_count": 620,
        "good_count": 615,
        "planned_production_time": 60.0,
        "actual_run_time": 60.0,
        "downtime_minutes": 0.0,
    })


_CONVEYOR_META = {
    "type": "Conveyor Motor", "criticality": "High", "nominal_vibration": 1.5,
    "nominal_temperature": 55.0, "nominal_rpm": 960.0, "install_date": "2017-05-08",
}


def test_missing_maintenance_history_is_neutral_not_overdue():
    """No record means unknown. The former 999-day sentinel put the row in a
    training region with a 0.0 failure rate, so a machine with no maintenance
    records scored 0% risk however bad its sensors were."""
    from app.ml.feature_engineering import NEUTRAL_DAYS_SINCE_MAINTENANCE, compute_maintenance_context

    context = compute_maintenance_context(pd.Timestamp.utcnow(), pd.DataFrame())
    assert context["days_since_last_maintenance"] == NEUTRAL_DAYS_SINCE_MAINTENANCE
    assert context["maintenance_context_available"] is False
    assert context["days_since_last_maintenance"] < 100  # never a sentinel again


def test_maintenance_history_present_is_measured():
    from app.ml.feature_engineering import compute_maintenance_context

    as_of = pd.Timestamp("2026-03-01")
    maintenance = pd.DataFrame([
        {"machine_id": 1, "maintenance_date": pd.Timestamp("2026-02-01"), "failure_mode": "BEARING_DEGRADATION"},
    ])
    context = compute_maintenance_context(as_of, maintenance)
    assert context["maintenance_context_available"] is True
    assert context["days_since_last_maintenance"] == 28.0
    assert context["previous_failure_count"] == 1


def test_absent_maintenance_records_do_not_change_the_score():
    """The core guarantee for uploaded data: whether maintenance records happen to
    be supplied must not move the risk score."""
    from app.ml.predict import predict_risk_from_readings

    maintenance = pd.DataFrame([{
        "machine_id": 1,
        "maintenance_date": datetime.utcnow() - timedelta(days=29),
        "failure_mode": None,
    }])

    for vibration, temperature, rpm in [(1.5, 55, 960), (2.8, 80, 935)]:
        readings = _noisy_readings(vibration, temperature, rpm)
        with_records = predict_risk_from_readings(readings, _CONVEYOR_META, maintenance)
        without = predict_risk_from_readings(readings, _CONVEYOR_META, pd.DataFrame())
        assert with_records == without, (vibration, temperature)


def test_out_of_range_input_is_clamped_and_reported():
    """A tree ensemble cannot extrapolate, so inputs beyond training are clamped
    and the exceedance is reported rather than silently extrapolated."""
    from app.ml.predict import predict_risk_with_attribution

    maintenance = pd.DataFrame([{
        "machine_id": 1,
        "maintenance_date": datetime.utcnow() - timedelta(days=29),
        "failure_mode": None,
    }])

    _, _, exceedances = predict_risk_with_attribution(
        _noisy_readings(2.8, 400, 935), _CONVEYOR_META, maintenance
    )
    assert exceedances, "an out-of-range reading must be reported as clamped"
    assert all(e["direction"] in ("above_training_range", "below_training_range") for e in exceedances)


def test_model_non_monotonicity_is_caught_by_the_deviation_signal():
    """The fitted model is non-monotonic INSIDE its own training range: a Conveyor
    Motor at 95 C scores near zero while the same machine at 90 C scores ~98%.
    Clamping cannot fix that, so the guarantee is that the second signal catches it
    and the disagreement is flagged rather than the machine being called safe.
    """
    from app.ml.predict import predict_risk_from_readings
    from app.services.anomaly_service import detect_signal_disagreement, deviation_score

    maintenance = pd.DataFrame([{
        "machine_id": 1,
        "maintenance_date": datetime.utcnow() - timedelta(days=29),
        "failure_mode": None,
    }])
    reference = {"vibration": 1.5, "temperature": 55.0, "rpm": 960.0}

    ml_risk = predict_risk_from_readings(
        _noisy_readings(2.8, 110, 935), _CONVEYOR_META, maintenance
    )
    deviation = deviation_score(
        {"vibration": 2.8, "temperature": 110.0, "rpm": 935.0}, reference
    )

    # The deviation signal must recognise a machine at twice its normal temperature.
    assert deviation["score"] >= 60.0

    # If the model understates it, that disagreement has to be surfaced.
    if ml_risk < 25.0:
        disagreement = detect_signal_disagreement(ml_risk, deviation["score"])
        assert disagreement is not None
        assert disagreement["kind"] == "model_understates"
        assert disagreement["recommended_signal"] == "deviation_score"


def test_deviation_score_is_monotonic():
    """The property the ML model lacks, and the reason this signal exists."""
    from app.services.anomaly_service import deviation_score

    reference = {"vibration": 1.5, "temperature": 55.0, "rpm": 960.0}
    ladder = [
        {"vibration": 1.5, "temperature": 55, "rpm": 960},
        {"vibration": 1.7, "temperature": 62, "rpm": 955},
        {"vibration": 2.0, "temperature": 70, "rpm": 950},
        {"vibration": 2.8, "temperature": 80, "rpm": 935},
        {"vibration": 4.2, "temperature": 92, "rpm": 880},
    ]
    scores = [deviation_score(c, reference)["score"] for c in ladder]
    assert scores == sorted(scores), scores
    assert scores[0] == 0.0
    assert scores[-1] > 50.0


def test_deviation_ignores_noise_and_handles_missing_reference():
    from app.services.anomaly_service import deviation_score

    reference = {"vibration": 1.5, "temperature": 55.0, "rpm": 960.0}
    # Small wobble around normal must not accumulate a score.
    assert deviation_score(
        {"vibration": 1.53, "temperature": 56.0, "rpm": 958}, reference
    )["score"] == 0.0
    # No reference at all cannot be scored, and must say so rather than return 0.
    result = deviation_score(
        {"vibration": 5.0, "temperature": 99.0, "rpm": 500},
        {"vibration": None, "temperature": None, "rpm": None},
    )
    assert result["method"] == "unavailable_no_reference"


def test_confidence_is_evidence_based_not_origin_based():
    """A well-populated upload must outrank a poorly-evidenced demo machine."""
    from app.services.anomaly_service import assess_confidence

    rich_upload = assess_confidence(
        reading_count=4000, machine_type="CNC Machine",
        reference_source="derived_baseline", maintenance_available=True,
    )
    thin_upload = assess_confidence(
        reading_count=10, machine_type="Robot Arm",
        reference_source="assumed_default", maintenance_available=False,
    )
    assert rich_upload["confidence"] == "high"
    assert thin_upload["confidence"] == "low"
    assert rich_upload["confidence_score"] > thin_upload["confidence_score"]
    # Every downgrade must be explained.
    assert thin_upload["reasons"]
    assert any("not in training" in r for r in thin_upload["reasons"])


def test_feature_ranges_are_published_for_every_feature():
    from app.ml.feature_engineering import FEATURE_COLUMNS
    from app.ml.guardrails import load_feature_ranges, reset_cache

    reset_cache()
    ranges = load_feature_ranges()
    assert ranges, "training must publish feature ranges for the inference guard"
    for column in FEATURE_COLUMNS:
        assert column in ranges, column
        assert ranges[column]["min"] <= ranges[column]["max"]


def test_clamping_leaves_in_range_values_untouched():
    from app.ml.guardrails import clamp_features, load_feature_ranges, reset_cache

    reset_cache()
    ranges = load_feature_ranges()
    assert ranges
    row = pd.DataFrame([{c: (b["min"] + b["max"]) / 2 for c, b in ranges.items()}])
    clamped, exceedances = clamp_features(row)
    assert exceedances == []
    pd.testing.assert_frame_equal(clamped, row)


# ─── Investigation ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_investigation_suggestions_cover_every_intent(client):
    from app.llm.investigate_service import INTENTS

    body = (await client.get("/api/investigate/suggestions")).json()
    assert set(body["intents"]) == set(INTENTS)
    assert body["questions"]


@pytest.mark.parametrize(
    "question,expected",
    [
        ("Why is M-102 at risk?", "why_at_risk"),
        ("Which machines are trending worse?", "trending_up"),
        ("Has this happened before?", "similar_past_failures"),
        ("Do we have the spare part in stock?", "part_readiness"),
        ("What is our financial exposure?", "cost_exposure"),
        ("Where are we losing OEE?", "oee_losses"),
        ("Give me a plant overview", "fleet_summary"),
    ],
)
def test_keyword_intent_routing(question, expected):
    from app.llm.investigate_service import classify_intent_keywords

    assert classify_intent_keywords(question) == expected


@pytest.mark.asyncio
async def test_investigation_answers_every_intent_with_evidence(client):
    questions = [
        "Why is M-102 at risk?",
        "Which machines are trending worse?",
        "Has this failure happened before?",
        "Do we have the parts we need?",
        "What is our cost exposure?",
        "Where are we losing the most OEE?",
        "Give me a plant overview",
    ]
    for question in questions:
        response = await client.post("/api/investigate", json={"question": question})
        assert response.status_code == 200, question
        body = response.json()
        # With no API key configured the deterministic path must answer.
        assert body["source"] in ("deterministic", "gemini", "cached")
        assert len(body["answer"]) > 20, question
        assert body["intent"]


@pytest.mark.asyncio
async def test_investigation_reports_current_state_not_resolved_alerts(client):
    """A resolved alert must not be described as if it were current — that made
    the dashboard and the answer contradict each other."""
    await init_db()
    async with AsyncSessionLocal() as db:
        machine = Machine(
            name="ZZ-RESOLVED-TEST", type="CNC Machine", location="Line A",
            install_date="2020-01-01", criticality="Medium",
            nominal_vibration=2.0, nominal_temperature=65.0, nominal_rpm=1450.0,
            cost_center_code="CC-100",
        )
        db.add(machine)
        await db.commit()
        await db.refresh(machine)
        machine_id = machine.id

        db.add(SensorReading(
            machine_id=machine_id, timestamp=datetime.utcnow(),
            vibration=2.0, temperature=65.0, rpm=1450.0, machine_status="Running",
            production_count=600, good_count=595, planned_production_time=60.0,
            actual_run_time=60.0, downtime_minutes=0.0, risk_score=3.0,
        ))
        db.add(Alert(
            machine_id=machine_id, risk_score=97.0, severity="Critical",
            maintenance_priority=90.0, status="Resolved",
            failure_mode="BEARING_DEGRADATION", created_at=datetime.utcnow(),
        ))
        await db.commit()

    try:
        body = (await client.post(
            "/api/investigate",
            json={"question": "Why is ZZ-RESOLVED-TEST at risk?"},
        )).json()
        answer = body["answer"].lower()
        # The current state must lead. A past incident may be mentioned, but only
        # as history — never presented as the machine's present risk.
        assert "not currently flagged" in answer or "normal" in answer
        if "97" in answer:
            assert "resolved" in answer
    finally:
        async with AsyncSessionLocal() as db:
            await db.execute(delete(Alert).where(Alert.machine_id == machine_id))
            await db.execute(delete(SensorReading).where(SensorReading.machine_id == machine_id))
            await db.execute(delete(Machine).where(Machine.id == machine_id))
            await db.commit()


@pytest.mark.asyncio
async def test_investigation_refuses_an_unresolvable_machine_name(client):
    """Naming a machine that does not exist must be refused, not silently answered
    about a different asset — that would be a wrong answer wearing a confident tone.
    The reply also lists the names that do exist, because "I do not have that" is
    only actionable alongside what is available."""
    body = (await client.post(
        "/api/investigate", json={"question": "Why is M-993 at risk?"}
    )).json()
    answer = body["answer"].lower()
    assert "machine name i do not have" in answer
    assert "m-102" in answer


@pytest.mark.asyncio
async def test_investigation_answers_a_risk_question_with_no_machine_named(client):
    """"Which machine is at high risk now and why?" names no machine but is exactly
    the question an operator asks first. It used to be refused. It must now be
    answered from the fleet's worst machine, and the answer must disclose that the
    machine was chosen rather than asked for."""
    body = (await client.post(
        "/api/investigate",
        json={"question": "which machine is at high risk now and why?"},
    )).json()
    answer = body["answer"]
    assert body["intent"] == "why_at_risk"
    assert "could not tell which machine" not in answer.lower()
    assert "did not name a machine" in answer.lower()
    assert "ranks highest" in answer.lower()

    async with AsyncSessionLocal() as db:
        names = [m.name for m in (await db.execute(select(Machine))).scalars().all()]
    assert any(name in answer for name in names), "must name the machine it chose"


@pytest.mark.asyncio
async def test_plural_risk_selector_is_answered_as_a_ranked_list(client):
    """A plural selector wants a list. Answering "which machines are at risk" with a
    single machine's root cause would be a partial answer to a fleet question."""
    body = (await client.post(
        "/api/investigate", json={"question": "which machines are at risk?"}
    )).json()
    assert body["intent"] == "fleet_summary"


@pytest.mark.asyncio
async def test_investigation_validates_input(client):
    assert (await client.post("/api/investigate", json={"question": ""})).status_code == 400
    assert (await client.post("/api/investigate", json={"question": "x" * 600})).status_code == 400


def test_deterministic_answers_never_raise():
    """The deterministic path is the guarantee; it has to survive empty evidence."""
    from app.llm.investigate_service import INTENTS, deterministic_answer

    for intent in INTENTS:
        assert isinstance(deterministic_answer(intent, {}), str)


# ─── Auth, roles and audit ────────────────────────────────────────────────────

def test_password_hashing_is_salted_and_verifiable():
    from app.services.auth_service import hash_password, verify_password

    salt, digest = hash_password("correct-horse")
    assert verify_password("correct-horse", salt, digest)
    assert not verify_password("wrong", salt, digest)
    # Same password, different salt, different hash.
    other_salt, other_digest = hash_password("correct-horse")
    assert other_digest != digest


def test_token_roundtrip_and_tamper_detection():
    from app.services.auth_service import create_token, decode_token

    token = create_token("planner", "planner")
    payload = decode_token(token)
    assert payload["sub"] == "planner"
    assert payload["role"] == "planner"

    # A tampered payload must not validate.
    body, signature = token.split(".", 1)
    assert decode_token(f"{body}x.{signature}") is None
    assert decode_token("garbage") is None
    assert decode_token("") is None


def test_expired_token_is_rejected():
    import base64
    import json as _json
    import time

    from app.services.auth_service import _sign, decode_token

    payload = {"sub": "planner", "role": "planner", "exp": int(time.time()) - 10}
    body = base64.urlsafe_b64encode(_json.dumps(payload).encode()).decode().rstrip("=")
    assert decode_token(f"{body}.{_sign(body)}") is None


def test_role_permissions_are_least_privilege():
    from app.services.auth_service import ROLE_PERMISSIONS

    assert ROLE_PERMISSIONS["viewer"] == set()
    # A technician progresses work but does not raise it.
    assert "workorder:create" not in ROLE_PERMISSIONS["technician"]
    assert "workorder:update" in ROLE_PERMISSIONS["technician"]
    assert "workorder:create" in ROLE_PERMISSIONS["planner"]


@pytest.mark.asyncio
async def test_login_rejects_bad_credentials_without_enumeration(client):
    """Unknown user and wrong password must be indistinguishable."""
    unknown = await client.post("/api/auth/login", json={"username": "nobody", "password": "x"})
    wrong = await client.post("/api/auth/login", json={"username": "planner", "password": "wrong"})
    assert unknown.status_code == 401
    assert wrong.status_code == 401
    assert unknown.json()["detail"] == wrong.json()["detail"]


@pytest.mark.asyncio
async def test_login_issues_token_with_permissions(client):
    from app.config import get_settings

    response = await client.post(
        "/api/auth/login",
        json={"username": "planner", "password": get_settings().demo_password},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["role"] == "planner"
    assert "workorder:create" in body["permissions"]
    assert body["token"]


@pytest.mark.asyncio
async def test_me_reports_unauthenticated_without_erroring(client):
    """The UI needs to ask "who am I" without treating "nobody" as a failure."""
    body = (await client.get("/api/auth/me")).json()
    assert body["authenticated"] is False
    assert body["role"] == "viewer"


@pytest.mark.asyncio
async def test_permission_gate_enforces_roles_when_enabled(client):
    """Exercised with auth switched on explicitly, since it ships off by default."""
    from fastapi import HTTPException

    from app.services.auth_service import create_token, require_permission

    class _Request:
        def __init__(self, token=None):
            self.headers = {"Authorization": f"Bearer {token}"} if token else {}

    from app.services import auth_service

    original = auth_service.settings.auth_enabled
    auth_service.settings.auth_enabled = True
    try:
        gate = require_permission("workorder:create")

        with pytest.raises(HTTPException) as no_token:
            await gate(_Request())
        assert no_token.value.status_code == 401

        with pytest.raises(HTTPException) as viewer:
            await gate(_Request(create_token("viewer", "viewer")))
        assert viewer.value.status_code == 403

        allowed = await gate(_Request(create_token("planner", "planner")))
        assert allowed["sub"] == "planner"
    finally:
        auth_service.settings.auth_enabled = original


@pytest.mark.asyncio
async def test_permission_gate_is_transparent_when_disabled(client):
    from app.services import auth_service
    from app.services.auth_service import require_permission

    class _Request:
        headers: dict = {}

    original = auth_service.settings.auth_enabled
    auth_service.settings.auth_enabled = False
    try:
        assert await require_permission("workorder:create")(_Request()) is None
    finally:
        auth_service.settings.auth_enabled = original


@pytest.mark.asyncio
async def test_mutations_write_an_audit_entry(client):
    """Acknowledging an alert must leave an accountable record."""
    await init_db()
    async with AsyncSessionLocal() as db:
        machine = (await db.execute(select(Machine).limit(1))).scalar_one()
        alert = Alert(
            machine_id=machine.id, risk_score=88.0, severity="Critical",
            maintenance_priority=90.0, status="Active",
            failure_mode="BEARING_DEGRADATION", created_at=datetime.utcnow(),
        )
        db.add(alert)
        await db.commit()
        await db.refresh(alert)
        alert_id = alert.id

    try:
        response = await client.patch(f"/api/alerts/{alert_id}", json={"status": "Acknowledged"})
        assert response.status_code == 200

        async with AsyncSessionLocal() as db:
            entry = (await db.execute(
                select(AuditLog)
                .where(AuditLog.entity_type == "alert")
                .where(AuditLog.entity_id == alert_id)
                .order_by(AuditLog.timestamp.desc())
            )).scalars().first()

        assert entry is not None
        assert entry.action == "ALERT_ACKNOWLEDGED"
        assert "Active -> Acknowledged" in (entry.details or "")
    finally:
        async with AsyncSessionLocal() as db:
            await db.execute(delete(AuditLog).where(AuditLog.entity_id == alert_id))
            await db.execute(delete(Alert).where(Alert.id == alert_id))
            await db.commit()


@pytest.mark.asyncio
async def test_audit_endpoint_returns_newest_first(client):
    rows = (await client.get("/api/auth/audit", params={"limit": 5})).json()
    if len(rows) > 1:
        stamps = [r["timestamp"] for r in rows]
        assert stamps == sorted(stamps, reverse=True)


# ─── Notifications ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_notification_skipped_when_unconfigured():
    """"Not configured" must be recorded, not silently ignored."""
    from app.services import notification_service

    await init_db()
    original = notification_service.settings.notification_webhook_url
    notification_service.settings.notification_webhook_url = ""
    try:
        async with AsyncSessionLocal() as db:
            status = await notification_service.notify_critical_alert(
                db, None, "ZZ-NOTIFY", 90.0, "OVERHEATING", 1000.0
            )
            await db.commit()
        assert status == "skipped"
    finally:
        notification_service.settings.notification_webhook_url = original
        async with AsyncSessionLocal() as db:
            await db.execute(delete(NotificationLog).where(NotificationLog.alert_id.is_(None)))
            await db.commit()


@pytest.mark.asyncio
async def test_notification_failure_is_logged_not_raised():
    """An unreachable webhook must never stop an alert being created."""
    from app.services import notification_service

    await init_db()
    original = notification_service.settings.notification_webhook_url
    notification_service.settings.notification_webhook_url = "http://127.0.0.1:59999/nope"
    try:
        async with AsyncSessionLocal() as db:
            status = await notification_service.notify_critical_alert(
                db, None, "ZZ-NOTIFY", 90.0, "OVERHEATING", 1000.0
            )
            await db.commit()
        assert status == "failed"
    finally:
        notification_service.settings.notification_webhook_url = original
        async with AsyncSessionLocal() as db:
            await db.execute(delete(NotificationLog).where(NotificationLog.alert_id.is_(None)))
            await db.commit()


# ─── Model transparency ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_model_endpoint_publishes_selection_and_metrics(client):
    body = (await client.get("/api/model")).json()
    assert body["available"] is True
    assert body["model_loaded"] is True
    assert body["selected_model"]
    assert body["selection_rule"]
    metrics = body["selected_metrics"]
    assert 0 <= metrics["recall"] <= 1
    assert 0 <= metrics["precision"] <= 1
    # Every candidate is published, including the ones that were rejected.
    assert len(body["all_candidates"]) >= 3
    assert body["global_importance"]


@pytest.mark.asyncio
async def test_model_endpoint_publishes_risk_bands_matching_the_rules(client):
    """Published thresholds must be the ones actually applied."""
    from app.services.rules_engine import classify_risk

    bands = (await client.get("/api/model")).json()["risk_bands"]
    for band in bands:
        assert classify_risk(band["min"]) == band["severity"]
        assert classify_risk(band["max"]) == band["severity"]


def test_calibration_is_rejected_when_it_costs_recall():
    """Spec §22 prioritises recall, so tidier probabilities must not win on their
    own. Guards the selection rule against silently preferring calibration."""
    import json as _json
    from pathlib import Path

    report_path = Path("app/ml/model_report.json")
    if not report_path.exists():
        pytest.skip("model report not generated")

    report = _json.loads(report_path.read_text())
    results = report["results"]
    selected = report["selected_model"]

    calibrated = results.get("Logistic Regression (calibrated)")
    if calibrated is None:
        pytest.skip("calibrated candidate not present")

    if calibrated["recall"] < results[selected]["recall"]:
        assert selected != "Logistic Regression (calibrated)"
