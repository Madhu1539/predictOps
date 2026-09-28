"""
Simulated Live Feed — spec §41.
Backend timer generates next synthetic sensor reading,
stores in DB, recalculates risk, updates alerts.
No streaming infrastructure needed.
"""
import asyncio
import logging
import json
import random
import numpy as np
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from app.database import AsyncSessionLocal
from app.models.machine import Machine, ORIGIN_SIMULATED
from app.models.sensor import SensorReading
from app.models.alert import Alert
from app.models.maintenance import MaintenanceRecord
from app.services.rules_engine import (
    probability_to_risk_score, classify_risk, should_create_alert, infer_failure_mode
)
from app.services.priority_service import (
    calculate_maintenance_priority,
    get_production_impact,
    resolve_production_impact,
)
from app.services.recommendation_service import get_recommendation, get_required_part
from app.services.oee_service import calculate_oee
from app.services.cost_service import estimate_downtime_cost, estimate_loss_avoided
from app.ml.attribution import top_sensors_from_attribution
from app.ml.feature_engineering import NEUTRAL_DAYS_SINCE_MAINTENANCE
from app.services.baseline_service import reference_for
from app.services.anomaly_service import (
    absolute_concerns,
    absolute_score,
    assess_confidence,
    detect_signal_disagreement,
    deviation_score,
)
from app.models.oee import OeeSnapshot
from app.models.cost_center import CostCenter
from app.models.material import Material

logger = logging.getLogger(__name__)

# Live-feed drift controls. A machine is treated as degrading once its vibration
# sits meaningfully above nominal; ceilings stop any machine drifting forever.
DEGRADING_VIBRATION_RATIO = 1.15
VIBRATION_CEILING_RATIO = 2.5
TEMPERATURE_CEILING_RATIO = 1.6
MEAN_REVERSION = 0.05

# OEE is aggregated over this many readings (24 hourly readings = one day).
OEE_WINDOW_READINGS = 24

# Deviation at or above this raises an alert even when the model scores low. The
# fitted model is non-monotonic at extreme sensor values, so without this a machine
# whose sensors are clearly abnormal could produce no alert at all.
DEVIATION_ALERT_THRESHOLD = 60.0

# Each reading represents one hourly production slot, matching the seeded data.
SLOT_MINUTES = 60.0


def _generate_next_reading(machine, last_reading, degradation_factor: float | None = None):
    """Generate the next correlated sensor reading based on the last one.

    Machines that are already degrading keep trending upward; healthy machines
    fluctuate around their nominal baseline instead of drifting without bound.
    Unbounded compounding drift would eventually push every machine to Critical
    and destroy the demo's signal (spec §41, §48).
    """
    noise_vib = np.random.normal(0, 0.1)
    noise_temp = np.random.normal(0, 0.3)
    noise_rpm = np.random.normal(0, 5)

    nominal_vib = max(machine.nominal_vibration, 0.01)
    nominal_temp = max(machine.nominal_temperature, 0.01)

    # Infer whether this machine is on a degradation path from its own history.
    if degradation_factor is None:
        already_degrading = (last_reading.vibration / nominal_vib) > DEGRADING_VIBRATION_RATIO
        degradation_factor = 1.0 if already_degrading else 0.0

    if degradation_factor > 0:
        # Continue degrading, but never beyond a physical ceiling.
        vib = min(
            last_reading.vibration * (1 + 0.002 * degradation_factor),
            nominal_vib * VIBRATION_CEILING_RATIO,
        )
        temp = min(
            last_reading.temperature * (1 + 0.001 * degradation_factor),
            nominal_temp * TEMPERATURE_CEILING_RATIO,
        )
    else:
        # Healthy: revert gently toward nominal so noise cannot accumulate.
        vib = last_reading.vibration + MEAN_REVERSION * (nominal_vib - last_reading.vibration)
        temp = last_reading.temperature + MEAN_REVERSION * (nominal_temp - last_reading.temperature)

    vib = max(0.1, vib + noise_vib)
    temp = max(20.0, temp + noise_temp)
    rpm = max(100.0, last_reading.rpm + noise_rpm)

    # Simulate occasional downtime within the hourly slot.
    downtime = 0.0
    machine_status = "Running"
    if random.random() < 0.05:
        downtime = random.uniform(5, 30)
        machine_status = "Idle"

    # Production is reported on the same basis as the seeded history: one hourly
    # slot, not a full shift. Mixing a 480-minute planned time into a window of
    # 60-minute readings makes the aggregated OEE meaningless.
    actual_run = max(0.0, SLOT_MINUTES - downtime)
    prod_count = int(actual_run * random.uniform(8, 12))
    good_count = int(prod_count * random.uniform(0.93, 0.99))

    return {
        "vibration": round(vib, 3),
        "temperature": round(temp, 2),
        "rpm": round(rpm, 1),
        "machine_status": machine_status,
        "production_count": prod_count,
        "good_count": good_count,
        "planned_production_time": SLOT_MINUTES,
        "actual_run_time": round(actual_run, 1),
        "downtime_minutes": round(downtime, 1),
    }


async def _compute_watch_streak(db: AsyncSession, machine_id: int, current_risk: float) -> int:
    """Count trailing consecutive readings where risk stayed in the Watch band
    and kept rising, including the current one (spec §26).

    A streak of 3 or more is what promotes a Watch condition into an alert.
    """
    result = await db.execute(
        select(SensorReading.risk_score)
        .where(SensorReading.machine_id == machine_id)
        .where(SensorReading.risk_score.isnot(None))
        .order_by(SensorReading.timestamp.desc())
        .limit(10)
    )
    previous = [row[0] for row in result.fetchall()]  # newest first

    streak = 1  # the current reading is itself in the Watch band
    later_value = current_risk
    for value in previous:
        if classify_risk(value) != "Watch" or value >= later_value:
            break
        streak += 1
        later_value = value
    return streak


async def _resolve_active_alerts(db: AsyncSession, machine_id: int) -> None:
    """Close out stale Active alerts once a machine returns to Normal.

    Without this an alert keeps its last elevated risk score forever.
    """
    result = await db.execute(
        select(Alert)
        .where(Alert.machine_id == machine_id)
        .where(Alert.status == "Active")
    )
    for alert in result.scalars().all():
        alert.status = "Resolved"


async def update_machine_risk(db: AsyncSession, machine: Machine, reading: SensorReading | None = None):
    """Calculate new risk score and update/create alert if needed.

    Public because ingestion re-scores a machine immediately after readings
    arrive, using exactly the same path as the simulator.
    """
    try:
        # Get last 48 readings
        result = await db.execute(
            select(SensorReading)
            .where(SensorReading.machine_id == machine.id)
            .order_by(SensorReading.timestamp.desc())
            .limit(48)
        )
        readings = result.scalars().all()
        if not readings:
            return
        # The query returns newest-first; flip to chronological order so that
        # `readings[-1]` really is the most recent reading. Without this the
        # alert context, deviations, and OEE snapshot all describe the OLDEST
        # reading in the window.
        readings = list(reversed(readings))

        import pandas as pd
        readings_df = pd.DataFrame([{
            "machine_id": r.machine_id,
            "timestamp": r.timestamp,
            "vibration": r.vibration,
            "temperature": r.temperature,
            "rpm": r.rpm,
            "machine_status": r.machine_status,
            "production_count": r.production_count,
            "good_count": r.good_count,
            "planned_production_time": r.planned_production_time,
            "actual_run_time": r.actual_run_time,
            "downtime_minutes": r.downtime_minutes,
        } for r in readings])
        readings_df = readings_df.sort_values("timestamp")

        # Get maintenance records
        maint_result = await db.execute(
            select(MaintenanceRecord)
            .where(MaintenanceRecord.machine_id == machine.id)
            .order_by(MaintenanceRecord.maintenance_date.desc())
        )
        maintenance_records = maint_result.scalars().all()
        maint_df = pd.DataFrame([{
            "machine_id": m.machine_id,
            "maintenance_date": m.maintenance_date,
            "failure_mode": m.failure_mode,
        } for m in maintenance_records]) if maintenance_records else pd.DataFrame()

        machine_meta = {
            "type": machine.type,
            "criticality": machine.criticality,
            "nominal_vibration": machine.nominal_vibration,
            "nominal_temperature": machine.nominal_temperature,
            "nominal_rpm": machine.nominal_rpm,
            "install_date": machine.install_date,
        }

        # Which sensor channels this machine actually has. A real plant rarely
        # instruments every one, and the model needs all three: it was fitted on
        # vibration, temperature and RPM together, so feeding it a substitute for a
        # channel that does not exist would produce a confident number with nothing
        # behind it. Better to report the score as unavailable and let the two
        # model-free signals carry the analysis.
        latest = readings[-1]
        present_sensors = [
            name for name in ("vibration", "temperature", "rpm")
            if getattr(latest, name) is not None
        ]
        model_scoreable = len(present_sensors) == 3

        risk_score = None
        attribution_records = []
        range_exceedances = []
        degraded_mode = False

        if not model_scoreable:
            degraded_mode = True
        else:
            # Get risk score
            try:
                from app.ml.predict import predict_risk_with_attribution
                risk_score, attribution_records, range_exceedances = predict_risk_with_attribution(
                    readings_df, machine_meta, maint_df
                )
            except Exception as e:
                logger.warning(f"ML prediction failed for machine {machine.id}: {e}")
                from app.ml.predict import rules_based_risk_fallback
                # Neutral rather than 999: the rules fallback treats a large value as
                # badly overdue, which would be an invented claim when no record exists.
                days_maint = NEUTRAL_DAYS_SINCE_MAINTENANCE
                if not maint_df.empty:
                    last_maint = pd.to_datetime(maint_df["maintenance_date"]).max()
                    days_maint = (datetime.utcnow() - last_maint.to_pydatetime()).days
                risk_score = rules_based_risk_fallback(
                    latest.vibration, machine.nominal_vibration,
                    latest.temperature, machine.nominal_temperature,
                    days_maint,
                )
                attribution_records = []
                range_exceedances = []
                degraded_mode = True

        severity = classify_risk(risk_score) if risk_score is not None else "Normal"

        # Persist the score on the reading so the risk trend and the Watch
        # streak rule both have real history to work from.
        watch_streak = 0
        if severity == "Watch" and risk_score is not None:
            watch_streak = await _compute_watch_streak(db, machine.id, risk_score)
        if reading is not None and risk_score is not None:
            reading.risk_score = risk_score

        # Deviation is computed BEFORE the alert decision, deliberately. The model
        # is non-monotonic at extreme sensor values (a Conveyor Motor at 95 C scores
        # ~0.1%), so gating on the model score alone would mean a machine whose
        # sensors are screaming produces no alert at all. The model-free signal gets
        # a vote on whether an alert exists, not merely on how it is described.
        reference = reference_for(machine)
        vib_baseline = reference["vibration"]
        temp_baseline = reference["temperature"]
        rpm_baseline = reference["rpm"]
        deviation = deviation_score(
            {"vibration": latest.vibration, "temperature": latest.temperature, "rpm": latest.rpm},
            {"vibration": vib_baseline, "temperature": temp_baseline, "rpm": rpm_baseline},
        )
        deviation_value = deviation["score"]
        # Only meaningful when there is an ML score to disagree with.
        disagreement = (
            detect_signal_disagreement(risk_score, deviation_value)
            if risk_score is not None else None
        )

        # Absolute limits catch what relative deviation cannot: a machine whose
        # entire supplied history was already degraded has a baseline that encodes
        # the fault as normal, so it scores zero deviation against itself.
        current_values = {
            "vibration": latest.vibration,
            "temperature": latest.temperature,
            "rpm": latest.rpm,
        }
        concerns = absolute_concerns(current_values)
        absolute_value = absolute_score(concerns)

        # Any of the three signals can raise an alert. Severity follows the model
        # score where there is one, and the model-free signals otherwise, so a
        # partially instrumented machine is still triaged rather than ignored.
        deviation_raises_alert = (
            deviation_value >= DEVIATION_ALERT_THRESHOLD or absolute_value >= 60.0
        )
        if deviation_raises_alert and severity == "Normal":
            severity = "Critical" if absolute_value >= 100.0 else "Warning"
        elif risk_score is None:
            # No model score at all: grade on the model-free evidence rather than
            # leaving an uninstrumented machine permanently at Normal.
            severity = classify_risk(max(deviation_value, absolute_value))

        if should_create_alert(risk_score or 0.0, watch_streak) or deviation_raises_alert:
            def _pct(current, baseline):
                """None when the channel is absent — a deviation cannot be computed
                against a measurement that was never taken."""
                if current is None or baseline is None:
                    return None
                return round((current - baseline) / max(baseline, 0.01) * 100, 1)

            vib_pct = _pct(latest.vibration, vib_baseline)
            temp_pct = _pct(latest.temperature, temp_baseline)
            # RPM deviation is signed: speed can drift below nominal as well as
            # above, and the direction matters diagnostically.
            rpm_pct = _pct(latest.rpm, rpm_baseline)

            # Days since maintenance. `None` means no record exists, which is
            # different from "a very long time" — the alert text must not claim
            # a machine is 999 days overdue when the records were never supplied.
            days_since_maint = None
            prev_failure = ""
            if not maint_df.empty:
                past = maint_df[pd.to_datetime(maint_df["maintenance_date"]) <= datetime.utcnow()]
                if not past.empty:
                    last_maint = pd.to_datetime(past["maintenance_date"]).max()
                    days_since_maint = (datetime.utcnow() - last_maint.to_pydatetime()).days
                    failures = past[past["failure_mode"].notna()]["failure_mode"].tolist()
                    if failures:
                        prev_failure = f"Previous {failures[-1].replace('_', ' ').title()} recorded"

            # Infer failure mode from the deviation profile (scale-free). Absent
            # channels contribute nothing rather than a zero, which would read as
            # "measured, and normal".
            rpm_std = readings_df["rpm"].std() if "rpm" in readings_df else None
            rpm_cv = (
                float(rpm_std / max(machine.nominal_rpm, 1.0))
                if rpm_std is not None and pd.notna(rpm_std) else 0.0
            )
            failure_mode = infer_failure_mode(
                vib_pct or 0.0, temp_pct or 0.0, rpm_cv, days_since_maint, machine.type
            )

            # Production impact from the ERP schedule where orders exist, falling
            # back to criticality where they do not. Deriving impact from
            # criticality alone made `calculate_maintenance_priority` multiply
            # criticality by a proxy for itself and ignore what is committed to run.
            impact_detail = await resolve_production_impact(
                db, machine.id, machine.criticality, latest.downtime_minutes
            )
            production_impact = impact_detail["impact"]
            # Priority needs a number. Where the model could not run, the model-free
            # evidence stands in, so an uninstrumented machine still gets ranked.
            priority_basis = (
                risk_score if risk_score is not None
                else max(deviation_value, absolute_value)
            )
            priority = calculate_maintenance_priority(priority_basis, machine.criticality, production_impact)
            recommended_action = get_recommendation(failure_mode)
            part_needed = get_required_part(failure_mode)

            # Check spare part availability
            from sqlalchemy import select as sa_select
            from app.models.spare_part import SparePart
            part_result = await db.execute(
                sa_select(SparePart).where(SparePart.name == part_needed)
            )
            part = part_result.scalar_one_or_none()
            # Fail closed: an unknown part cannot be assumed to be in stock.
            part_available = part.is_available if part else False

            sensor_deviations = json.dumps({
                "vibration_pct": vib_pct,
                "temperature_pct": temp_pct,
                # RPM is a named sensor in the problem statement and already
                # drives failure-mode inference, so it belongs in the payload the
                # operator and the explanation actually see.
                "rpm_pct": rpm_pct,
                "rpm_cv": round(rpm_cv, 4),
                # What the percentages are relative to: design_spec,
                # derived_baseline or assumed_default.
                "reference_source": reference["source"],
                # Channels this machine actually reports. A null percentage above
                # means "not instrumented", not "measured and normal".
                "present_sensors": present_sensors,
            })

            # Sensor ranking comes from the model's own contributions, not a
            # hardcoded ordering. Falls back to deviation size when attribution
            # is unavailable (degraded mode).
            ranked_sensors = top_sensors_from_attribution(attribution_records)
            if not ranked_sensors:
                ranked_sensors = [
                    name for name, magnitude in sorted(
                        (
                            ("vibration", abs(vib_pct) if vib_pct is not None else -1.0),
                            ("temperature", abs(temp_pct) if temp_pct is not None else -1.0),
                            ("rpm", abs(rpm_pct) if rpm_pct is not None else -1.0),
                        ),
                        key=lambda kv: kv[1],
                        reverse=True,
                    )
                    # Drop channels this machine does not report, rather than listing
                    # them as the least significant finding.
                    if magnitude >= 0.0
                ]
            top_sensors = json.dumps(ranked_sensors)
            attribution_json = json.dumps(attribution_records) if attribution_records else None

            # Second, independent signal was already computed above so it could
            # influence whether this alert exists at all.
            # Total stored history, not the 48-reading scoring window: a machine
            # with months of data must not be reported as having "limited history".
            total_readings = (
                await db.execute(
                    sa_select(func.count()).select_from(SensorReading)
                    .where(SensorReading.machine_id == machine.id)
                )
            ).scalar_one()

            confidence = assess_confidence(
                reading_count=int(total_readings or 0),
                machine_type=machine.type,
                reference_source=reference["source"],
                maintenance_available=days_since_maint is not None,
                range_exceedances=range_exceedances,
                missing_sensors=[
                    s for s in ("vibration", "temperature", "rpm")
                    if s not in present_sensors
                ],
            )
            deviation_json = json.dumps(deviation)
            confidence_level = confidence["confidence"]

            if concerns:
                # Recorded on the deviation payload so the report can show that an
                # absolute limit, not a relative one, is what flagged this machine.
                deviation["absolute_concerns"] = concerns
                deviation["absolute_score"] = absolute_value
                deviation_json = json.dumps(deviation)

            if disagreement is not None:
                confidence["signal_disagreement"] = disagreement
                if disagreement["kind"] == "model_understates":
                    confidence_level = "low"
                    confidence["confidence"] = "low"
            confidence_json = json.dumps(confidence)

            # Modelled business impact, using ERP cost data (see cost_service).
            cost_center = None
            if machine.cost_center_code:
                cc_result = await db.execute(
                    sa_select(CostCenter).where(CostCenter.code == machine.cost_center_code)
                )
                cost_center = cc_result.scalar_one_or_none()
            material_result = await db.execute(
                sa_select(Material).where(Material.part_name == part_needed)
            )
            material = material_result.scalar_one_or_none()

            estimated_downtime_cost = estimate_downtime_cost(
                latest.downtime_minutes, cost_center
            )
            estimated_loss_avoided = estimate_loss_avoided(
                # Loss avoided scales with likelihood. Without a model score the
                # model-free evidence is the best available estimate of that.
                priority_basis, failure_mode, cost_center, material
            )

            # Check if active alert exists
            alert_result = await db.execute(
                sa_select(Alert)
                .where(Alert.machine_id == machine.id)
                .where(Alert.status == "Active")
                .order_by(Alert.created_at.desc())
                .limit(1)
            )
            existing_alert = alert_result.scalar_one_or_none()

            if existing_alert:
                existing_alert.risk_score = risk_score
                existing_alert.severity = severity
                existing_alert.maintenance_priority = priority
                existing_alert.sensor_deviations = sensor_deviations
                existing_alert.failure_mode = failure_mode
                existing_alert.degraded_mode = degraded_mode
                # Everything derived from the failure mode must move with it,
                # otherwise the alert recommends a part for a mode it no longer
                # reports.
                existing_alert.top_sensors = top_sensors
                existing_alert.recommended_action = recommended_action
                existing_alert.part_needed = part_needed
                existing_alert.part_available = part_available
                existing_alert.days_since_maintenance = days_since_maint
                existing_alert.previous_failure_context = prev_failure
                existing_alert.production_impact = production_impact
                existing_alert.machine_criticality = machine.criticality
                existing_alert.estimated_downtime_cost = estimated_downtime_cost
                existing_alert.estimated_loss_avoided = estimated_loss_avoided
                existing_alert.attribution = attribution_json
                existing_alert.deviation_score = deviation_value
                existing_alert.deviation_detail = deviation_json
                existing_alert.confidence = confidence_level
                existing_alert.confidence_detail = confidence_json
            else:
                new_alert = Alert(
                    machine_id=machine.id,
                    risk_score=risk_score,
                    severity=severity,
                    maintenance_priority=priority,
                    top_sensors=top_sensors,
                    sensor_deviations=sensor_deviations,
                    failure_mode=failure_mode,
                    days_since_maintenance=days_since_maint,
                    previous_failure_context=prev_failure,
                    machine_criticality=machine.criticality,
                    production_impact=production_impact,
                    part_needed=part_needed,
                    part_available=part_available,
                    recommended_action=recommended_action,
                    status="Active",
                    degraded_mode=degraded_mode,
                    estimated_downtime_cost=estimated_downtime_cost,
                    estimated_loss_avoided=estimated_loss_avoided,
                    attribution=attribution_json,
                    deviation_score=deviation_value,
                    deviation_detail=deviation_json,
                    confidence=confidence_level,
                    confidence_detail=confidence_json,
                )
                db.add(new_alert)

                # A new Critical alert notifies outward. Fail-soft: delivery
                # problems are logged, never raised, so the alert exists either way.
                if severity == "Critical":
                    try:
                        await db.flush()
                        from app.services.notification_service import notify_critical_alert
                        await notify_critical_alert(
                            db,
                            alert_id=new_alert.id,
                            machine_name=machine.label,
                            risk_score=priority_basis,
                            failure_mode=failure_mode,
                            estimated_loss_avoided=estimated_loss_avoided,
                        )
                    except Exception as exc:
                        logger.warning(
                            f"Critical notification failed for {machine.name}: {exc}"
                        )

                # Close the loop: a qualifying prediction raises the work order
                # itself, so the machine is scheduled for maintenance without a
                # planner having to notice the alert first. Duplicate-guarded and
                # fail-soft inside `auto_create_work_order`.
                try:
                    await db.flush()
                    from app.services.workorder_service import auto_create_work_order
                    await auto_create_work_order(db, new_alert, machine.label)
                except Exception as exc:
                    logger.warning(
                        f"Automatic work order for {machine.name} failed: {exc}"
                    )

        elif severity == "Normal":
            # Risk has fallen back to normal operating condition — retire any
            # alert still sitting Active so it cannot show a frozen score.
            await _resolve_active_alerts(db, machine.id)

        # Store OEE snapshot. OEE is a rate measured over a period, not a single
        # instant: aggregating the last 24 readings (one day) gives a meaningful
        # availability figure instead of the 0-or-1 result a single hourly
        # reading produces.
        window = readings[-OEE_WINDOW_READINGS:]
        oee_vals = calculate_oee(
            sum(r.planned_production_time for r in window),
            sum(r.actual_run_time for r in window),
            sum(r.production_count for r in window),
            sum(r.good_count for r in window),
            sum(r.downtime_minutes for r in window),
            ideal_units_per_hour=machine.ideal_units_per_hour,
        )
        oee_snap = OeeSnapshot(
            machine_id=machine.id,
            timestamp=datetime.utcnow(),
            **oee_vals,
        )
        db.add(oee_snap)
        await db.commit()

    except Exception as e:
        logger.error(f"Error updating machine {machine.id} risk: {e}")
        await db.rollback()


async def live_feed_loop(interval_seconds: int = 15):
    """
    Background task: generates synthetic readings for the simulated fleet
    at a configurable interval.
    """
    logger.info(f"Live feed started with {interval_seconds}s interval")
    while True:
        try:
            async with AsyncSessionLocal() as db:
                # CORRECTNESS BOUNDARY — do not widen this filter.
                #
                # The simulator fabricates readings. Applying it to externally
                # supplied data would silently extend a real factory's history
                # with invented values, which is indistinguishable from falsifying
                # their data. Only machines the simulator owns may be extended.
                result = await db.execute(
                    select(Machine).where(Machine.data_origin == ORIGIN_SIMULATED)
                )
                machines = result.scalars().all()

                for machine in machines:
                    # Get latest reading
                    last_result = await db.execute(
                        select(SensorReading)
                        .where(SensorReading.machine_id == machine.id)
                        .order_by(SensorReading.timestamp.desc())
                        .limit(1)
                    )
                    last_reading = last_result.scalar_one_or_none()

                    if last_reading is None:
                        continue

                    # Generate new reading
                    new_data = _generate_next_reading(machine, last_reading)
                    new_reading = SensorReading(
                        machine_id=machine.id,
                        timestamp=datetime.utcnow(),
                        source="simulator",
                        **new_data,
                    )
                    db.add(new_reading)
                    await db.commit()
                    await db.refresh(new_reading)

                    # Update risk and alerts
                    await update_machine_risk(db, machine, new_reading)

        except Exception as e:
            logger.error(f"Live feed loop error: {e}")

        await asyncio.sleep(interval_seconds)
