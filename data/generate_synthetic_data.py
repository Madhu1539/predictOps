"""
Synthetic Data Generator — spec §13-15.
Generates 20 machines with correlated, realistic sensor data over 90 days.
Failure precursor patterns emerge from combinations, not simple thresholds.
"""
import sys
import os
# Add backend/ to path so 'app' package is importable
BACKEND_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'backend')
sys.path.insert(0, BACKEND_DIR)
os.chdir(BACKEND_DIR)  # Change to backend/ so relative DB path works

import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from sqlalchemy import create_engine, insert
from sqlalchemy.orm import Session

# We use synchronous engine here for data generation
from app.models.machine import Machine
from app.models.sensor import SensorReading
from app.models.maintenance import MaintenanceRecord
from app.models.spare_part import SparePart
from app.models.technician import Technician
from app.models.cost_center import CostCenter
from app.models.material import Material
from app.models.production_order import ProductionOrder
from app.database import Base, sync_database_url
# Single source of truth for the failure scenarios, shared with the training
# labels so generated data and labels can never drift apart.
from app.ml.labeling import FAILURE_SCENARIOS as LABELING_FAILURE_SCENARIOS, get_failure_day

np.random.seed(42)

# ─── Database URL ─────────────────────────────────────────────────────────────
# Derived from DATABASE_URL so the generator seeds whatever the application is
# configured to use, rather than only the local SQLite file. `SEED_DATABASE_URL`
# overrides it, which lets the Render build train a model against a throwaway
# SQLite file without touching the production database.
DB_URL = os.environ.get("SEED_DATABASE_URL") or sync_database_url()

# ─── Machine Definitions ─────────────────────────────────────────────────────
MACHINES = [
    # CNC Machines (5)
    {"name": "M-101", "type": "CNC Machine", "location": "Line A", "criticality": "High",
     "nominal_vibration": 1.8, "nominal_temperature": 62.0, "nominal_rpm": 1500,
     "install_date": "2019-03-15"},
    {"name": "M-102", "type": "CNC Machine", "location": "Line A", "criticality": "High",
     "nominal_vibration": 2.0, "nominal_temperature": 65.0, "nominal_rpm": 1450,
     "install_date": "2018-07-20"},  # DEMO MACHINE - will be high risk
    {"name": "M-103", "type": "CNC Machine", "location": "Line B", "criticality": "Medium",
     "nominal_vibration": 1.9, "nominal_temperature": 63.0, "nominal_rpm": 1480,
     "install_date": "2020-01-10"},
    {"name": "M-104", "type": "CNC Machine", "location": "Line B", "criticality": "High",
     "nominal_vibration": 2.1, "nominal_temperature": 67.0, "nominal_rpm": 1420,
     "install_date": "2017-11-05"},
    {"name": "M-105", "type": "CNC Machine", "location": "Line C", "criticality": "Low",
     "nominal_vibration": 1.7, "nominal_temperature": 60.0, "nominal_rpm": 1520,
     "install_date": "2021-06-30"},

    # Hydraulic Presses (5)
    {"name": "HP-201", "type": "Hydraulic Press", "location": "Press Shop", "criticality": "High",
     "nominal_vibration": 3.2, "nominal_temperature": 72.0, "nominal_rpm": 800,
     "install_date": "2016-04-12"},
    {"name": "HP-202", "type": "Hydraulic Press", "location": "Press Shop", "criticality": "Medium",
     "nominal_vibration": 2.9, "nominal_temperature": 70.0, "nominal_rpm": 820,
     "install_date": "2018-09-25"},
    {"name": "HP-203", "type": "Hydraulic Press", "location": "Press Shop", "criticality": "High",
     "nominal_vibration": 3.5, "nominal_temperature": 75.0, "nominal_rpm": 780,
     "install_date": "2015-12-01"},
    {"name": "HP-204", "type": "Hydraulic Press", "location": "Assembly", "criticality": "Medium",
     "nominal_vibration": 3.0, "nominal_temperature": 71.0, "nominal_rpm": 810,
     "install_date": "2019-08-14"},
    {"name": "HP-205", "type": "Hydraulic Press", "location": "Assembly", "criticality": "Low",
     "nominal_vibration": 2.8, "nominal_temperature": 68.0, "nominal_rpm": 830,
     "install_date": "2022-02-20"},

    # Conveyor Motors (5)
    {"name": "CM-301", "type": "Conveyor Motor", "location": "Line A", "criticality": "High",
     "nominal_vibration": 1.5, "nominal_temperature": 55.0, "nominal_rpm": 960,
     "install_date": "2017-05-08"},
    {"name": "CM-302", "type": "Conveyor Motor", "location": "Line B", "criticality": "Medium",
     "nominal_vibration": 1.4, "nominal_temperature": 53.0, "nominal_rpm": 980,
     "install_date": "2019-03-22"},
    {"name": "CM-303", "type": "Conveyor Motor", "location": "Line C", "criticality": "Low",
     "nominal_vibration": 1.6, "nominal_temperature": 57.0, "nominal_rpm": 950,
     "install_date": "2020-11-15"},
    {"name": "CM-304", "type": "Conveyor Motor", "location": "Warehouse", "criticality": "Medium",
     "nominal_vibration": 1.3, "nominal_temperature": 52.0, "nominal_rpm": 990,
     "install_date": "2021-07-03"},
    {"name": "CM-305", "type": "Conveyor Motor", "location": "Warehouse", "criticality": "High",
     "nominal_vibration": 1.5, "nominal_temperature": 56.0, "nominal_rpm": 970,
     "install_date": "2018-01-17"},

    # Industrial Pumps (5)
    {"name": "IP-401", "type": "Industrial Pump", "location": "Utility Room", "criticality": "High",
     "nominal_vibration": 2.4, "nominal_temperature": 58.0, "nominal_rpm": 1200,
     "install_date": "2016-09-30"},
    {"name": "IP-402", "type": "Industrial Pump", "location": "Utility Room", "criticality": "Medium",
     "nominal_vibration": 2.2, "nominal_temperature": 56.0, "nominal_rpm": 1220,
     "install_date": "2018-04-11"},
    {"name": "IP-403", "type": "Industrial Pump", "location": "Cooling Tower", "criticality": "High",
     "nominal_vibration": 2.6, "nominal_temperature": 60.0, "nominal_rpm": 1180,
     "install_date": "2017-07-28"},
    {"name": "IP-404", "type": "Industrial Pump", "location": "Cooling Tower", "criticality": "Medium",
     "nominal_vibration": 2.3, "nominal_temperature": 57.0, "nominal_rpm": 1210,
     "install_date": "2020-05-19"},
    {"name": "IP-405", "type": "Industrial Pump", "location": "Utility Room", "criticality": "Low",
     "nominal_vibration": 2.0, "nominal_temperature": 54.0, "nominal_rpm": 1230,
     "install_date": "2022-08-01"},
]

SPARE_PARTS = [
    {"name": "Bearing Assembly", "stock_quantity": 5, "minimum_stock": 1},
    {"name": "Motor Belt", "stock_quantity": 8, "minimum_stock": 2},
    {"name": "Cooling Fan", "stock_quantity": 3, "minimum_stock": 1},
    {"name": "Drive Coupling", "stock_quantity": 4, "minimum_stock": 1},
    {"name": "Lubricant", "stock_quantity": 20, "minimum_stock": 5},
]

TECHNICIANS = [
    {"name": "Technician A", "active_work_orders": 2},
    {"name": "Technician B", "active_work_orders": 0},
    {"name": "Technician C", "active_work_orders": 1},
    {"name": "Technician D", "active_work_orders": 3},
]

# ── ERP-side master data ─────────────────────────────────────────────────────
# This is the IT half of the IT/OT convergence: what downtime costs, what a part
# costs and how long it takes to arrive, and what is scheduled to be produced.
# Seeded here as ERP-shaped records; a real deployment would sync these from
# SAP/Dynamics (see erp_service.sync_from_erp for the intended seam).

COST_CENTERS = [
    {"code": "CC-100", "name": "Machining - Line A", "downtime_cost_per_hour": 4800.0},
    {"code": "CC-110", "name": "Machining - Line B", "downtime_cost_per_hour": 3900.0},
    {"code": "CC-120", "name": "Machining - Line C", "downtime_cost_per_hour": 2600.0},
    {"code": "CC-200", "name": "Press Shop", "downtime_cost_per_hour": 6500.0},
    {"code": "CC-210", "name": "Assembly", "downtime_cost_per_hour": 5200.0},
    {"code": "CC-300", "name": "Warehouse & Utilities", "downtime_cost_per_hour": 1400.0},
]

# Machine location → owning cost centre.
LOCATION_TO_COST_CENTER = {
    "Line A": "CC-100",
    "Line B": "CC-110",
    "Line C": "CC-120",
    "Press Shop": "CC-200",
    "Assembly": "CC-210",
    "Warehouse": "CC-300",
    "Utility Room": "CC-300",
}

# Design throughput by machine type, in units/hour. The generator produces
# 8-12 units/minute, so 720 units/hour is the design maximum it can reach.
TYPE_IDEAL_UNITS_PER_HOUR = {
    "CNC Machine": 720.0,
    "Hydraulic Press": 660.0,
    "Conveyor Motor": 720.0,
    "Industrial Pump": 690.0,
}

# ERP material master. `part_name` must match SPARE_PARTS names and the values
# in recommendation_service.PART_MAP.
MATERIALS = [
    {"part_name": "Bearing Assembly", "erp_material_no": "MAT-40021", "unit_cost": 480.0,
     "lead_time_days": 5, "supplier": "SKF Industrial"},
    {"part_name": "Motor Belt", "erp_material_no": "MAT-40088", "unit_cost": 130.0,
     "lead_time_days": 2, "supplier": "Gates Drive Systems"},
    {"part_name": "Cooling Fan", "erp_material_no": "MAT-40145", "unit_cost": 310.0,
     "lead_time_days": 7, "supplier": "EBM Thermal"},
    {"part_name": "Drive Coupling", "erp_material_no": "MAT-40203", "unit_cost": 265.0,
     "lead_time_days": 4, "supplier": "Rexnord"},
    {"part_name": "Lubricant", "erp_material_no": "MAT-40310", "unit_cost": 45.0,
     "lead_time_days": 1, "supplier": "Shell Lubricants"},
]

# Products produced per machine type, used to generate production orders.
TYPE_PRODUCTS = {
    "CNC Machine": ["Housing Bracket", "Spindle Mount", "Gear Blank"],
    "Hydraulic Press": ["Stamped Panel", "Chassis Rail", "Door Frame"],
    "Conveyor Motor": ["Transfer Assembly", "Belt Module"],
    "Industrial Pump": ["Coolant Circuit", "Hydraulic Manifold"],
}

FAILURE_SCENARIOS = LABELING_FAILURE_SCENARIOS

MAINTENANCE_HISTORY = {
    # machine_name: list of (days_ago, type, failure_mode, part_used, description)
    "M-102": [
        (63, "Preventive", None, "Lubricant", "Scheduled lubrication and inspection"),
        (125, "Corrective", "BEARING_DEGRADATION", "Bearing Assembly", "Bearing replaced after vibration alarm"),
        (200, "Preventive", None, "Motor Belt", "Belt replacement and alignment check"),
    ],
    "M-101": [(30, "Preventive", None, "Lubricant", "Routine lubrication")],
    "HP-201": [(45, "Corrective", "MOTOR_DEGRADATION", "Motor Belt", "Belt replaced after motor noise")],
    "HP-203": [(70, "Preventive", None, "Lubricant", "Scheduled service")],
    "CM-301": [(55, "Corrective", "BEARING_DEGRADATION", "Bearing Assembly", "Bearing replaced")],
    "IP-401": [(40, "Corrective", "OVERHEATING", "Cooling Fan", "Cooling fan replaced after overheating event")],
    "IP-403": [(65, "Preventive", None, "Lubricant", "Routine service")],
    # Most other machines — last maintenance within normal range
    "M-103": [(20, "Preventive", None, "Lubricant", "Scheduled")],
    "M-104": [(10, "Preventive", None, "Lubricant", "Scheduled")],
    "M-105": [(15, "Preventive", None, "Lubricant", "Scheduled")],
    "HP-202": [(25, "Preventive", None, "Lubricant", "Scheduled")],
    "HP-204": [(35, "Preventive", None, "Lubricant", "Scheduled")],
    "HP-205": [(18, "Preventive", None, "Lubricant", "Scheduled")],
    "CM-302": [(22, "Preventive", None, "Lubricant", "Scheduled")],
    "CM-303": [(12, "Preventive", None, "Lubricant", "Scheduled")],
    "CM-304": [(28, "Preventive", None, "Lubricant", "Scheduled")],
    "IP-402": [(19, "Preventive", None, "Lubricant", "Scheduled")],
    "IP-404": [(30, "Preventive", None, "Lubricant", "Scheduled")],
    "IP-405": [(8, "Preventive", None, "Lubricant", "Scheduled")],
}

DAYS = 90
READINGS_PER_DAY = 24  # hourly readings

# Ceilings for degraded sensor values, as a multiple of nominal. A failing
# machine runs hot and rough but within physically plausible bounds (spec §14).
VIBRATION_CEILING_RATIO = 2.5
TEMPERATURE_CEILING_RATIO = 1.6


def _generate_machine_sensor_data(machine_def: dict, start_date: datetime) -> list:
    """
    Generate 90 days × 24 hourly readings with correlated degradation.
    Failure precursor patterns emerge from combinations, not simple thresholds.
    """
    name = machine_def["name"]
    nominal_vib = machine_def["nominal_vibration"]
    nominal_temp = machine_def["nominal_temperature"]
    nominal_rpm = machine_def["nominal_rpm"]

    scenario = FAILURE_SCENARIOS.get(name)
    failure_day = get_failure_day(name)

    readings = []

    for day in range(DAYS):
        for hour in range(READINGS_PER_DAY):
            ts = start_date + timedelta(days=day, hours=hour)

            # Sensor levels are derived from nominal each reading via a bounded
            # ramp, rather than compounded step by step. Compounding either runs
            # away to implausible values or, once clamped, saturates weeks early
            # and erases the difference between "degrading" and "about to fail" —
            # which is exactly the signal the model must learn (spec §14-16).
            vib = nominal_vib
            temp = nominal_temp
            rpm = nominal_rpm
            rpm_instability = 0.0
            ramp = 0.0

            if scenario and failure_day is not None:
                failure_mode, start_day, severity = scenario
                if day >= start_day:
                    severity_mult = {"severe": 1.0, "moderate": 0.75, "mild": 0.45}[severity]
                    span = max(failure_day - start_day, 1)
                    # Accelerating ramp: slow early, steep close to failure.
                    progress = min((day - start_day) / span, 1.0)
                    ramp = (progress ** 1.6) * severity_mult

                    # Per-mode signature: which sensors move, and how much.
                    if failure_mode == "BEARING_DEGRADATION":
                        vib_share, temp_share, rpm_share = 1.0, 0.55, 0.15
                    elif failure_mode == "OVERHEATING":
                        vib_share, temp_share, rpm_share = 0.30, 1.0, 0.70
                    elif failure_mode == "MOTOR_DEGRADATION":
                        vib_share, temp_share, rpm_share = 0.65, 0.30, 1.0
                    elif failure_mode == "MISALIGNMENT":
                        vib_share, temp_share, rpm_share = 1.0, 0.10, 0.35
                    else:  # GENERAL_MECHANICAL_WEAR
                        vib_share, temp_share, rpm_share = 0.60, 0.45, 0.30

                    vib = nominal_vib * (1 + (VIBRATION_CEILING_RATIO - 1) * vib_share * ramp)
                    temp = nominal_temp * (1 + (TEMPERATURE_CEILING_RATIO - 1) * temp_share * ramp)
                    rpm_instability = nominal_rpm * 0.03 * rpm_share * ramp

            # Add realistic noise
            noise_vib = np.random.normal(0, nominal_vib * 0.04)
            noise_temp = np.random.normal(0, nominal_temp * 0.015)
            noise_rpm = np.random.normal(0, nominal_rpm * 0.01 + rpm_instability)

            reading_vib = max(0.01, vib + noise_vib)
            reading_temp = max(20.0, temp + noise_temp)
            reading_rpm = max(100.0, rpm + noise_rpm)

            # Production data
            downtime = 0.0
            machine_status = "Running"
            if ramp > 0:
                # Downtime becomes more frequent as the machine degrades.
                if np.random.random() < min(0.15, ramp * 0.12):
                    downtime = np.random.uniform(10, 60)
                    machine_status = "Idle"
            elif np.random.random() < 0.03:
                downtime = np.random.uniform(5, 20)
                machine_status = "Idle"

            actual_run = max(0, 60.0 - downtime)  # 60 min per hour slot
            prod_count = int(actual_run * np.random.uniform(8, 12))
            good_count = int(prod_count * np.random.uniform(0.93, 0.99))

            readings.append({
                "timestamp": ts,
                "vibration": round(reading_vib, 3),
                "temperature": round(reading_temp, 2),
                "rpm": round(reading_rpm, 1),
                "machine_status": machine_status,
                "production_count": prod_count,
                "good_count": good_count,
                "planned_production_time": 60.0,
                "actual_run_time": round(actual_run, 1),
                "downtime_minutes": round(downtime, 1),
            })

    return readings


def generate_and_seed():
    """Main function: generates all synthetic data and seeds the database."""
    engine = create_engine(DB_URL)
    Base.metadata.create_all(engine)

    start_date = datetime.utcnow() - timedelta(days=DAYS)

    with Session(engine) as session:
        # Check if data already exists
        existing = session.query(Machine).count()
        if existing > 0:
            print(f"Database already has {existing} machines. Skipping generation.")
            print("To regenerate, delete predictops.db and re-run.")
            return

        print("Generating synthetic data...")

        # ── Spare Parts ──────────────────────────────────────────────────────
        for sp in SPARE_PARTS:
            session.add(SparePart(**sp))
        session.flush()
        print(f"  [OK] {len(SPARE_PARTS)} spare parts")

        # ── Technicians ──────────────────────────────────────────────────────
        for t in TECHNICIANS:
            session.add(Technician(**t))
        session.flush()
        print(f"  [OK] {len(TECHNICIANS)} technicians")

        # ── ERP: Cost Centres ────────────────────────────────────────────────
        for cc in COST_CENTERS:
            session.add(CostCenter(**cc))
        session.flush()
        print(f"  [OK] {len(COST_CENTERS)} cost centres (ERP)")

        # ── ERP: Material Master ─────────────────────────────────────────────
        for mat in MATERIALS:
            session.add(Material(**mat))
        session.flush()
        print(f"  [OK] {len(MATERIALS)} materials (ERP)")

        # ── Machines + Sensor Data + Maintenance ─────────────────────────────
        order_seq = 0
        pending_readings: list[dict] = []
        for machine_def in MACHINES:
            m = Machine(
                name=machine_def["name"],
                type=machine_def["type"],
                location=machine_def["location"],
                install_date=machine_def["install_date"],
                criticality=machine_def["criticality"],
                nominal_vibration=machine_def["nominal_vibration"],
                nominal_temperature=machine_def["nominal_temperature"],
                nominal_rpm=machine_def["nominal_rpm"],
                cost_center_code=LOCATION_TO_COST_CENTER.get(machine_def["location"], "CC-300"),
                ideal_units_per_hour=TYPE_IDEAL_UNITS_PER_HOUR.get(machine_def["type"], 720.0),
            )
            session.add(m)
            session.flush()

            # ── ERP: Production Orders ───────────────────────────────────────
            products = TYPE_PRODUCTS.get(machine_def["type"], ["General Part"])
            for i in range(3):
                order_seq += 1
                start = datetime.utcnow() - timedelta(days=(2 - i) * 7)
                planned = int(np.random.uniform(4000, 12000))
                # Orders in the past are complete; the newest is still running.
                if i < 2:
                    status = "Complete"
                    actual = int(planned * np.random.uniform(0.9, 1.0))
                else:
                    status = "InProgress"
                    actual = int(planned * np.random.uniform(0.3, 0.7))
                session.add(ProductionOrder(
                    order_no=f"PO-{order_seq:05d}",
                    machine_id=m.id,
                    product=products[i % len(products)],
                    planned_qty=planned,
                    actual_qty=actual,
                    scheduled_start=start,
                    scheduled_end=start + timedelta(days=6),
                    status=status,
                ))

            # Generate sensor readings.
            #
            # Collected as plain dicts and inserted through SQLAlchemy Core below
            # rather than as 43,200 ORM objects. The ORM path asks for generated
            # primary keys back and tracks every instance in the identity map, which
            # is wasted work for write-once rows: against a hosted database in
            # another region it did not finish within 25 minutes, because the cost is
            # dominated by network round trips rather than by the inserts.
            readings = _generate_machine_sensor_data(machine_def, start_date)
            pending_readings.extend({"machine_id": m.id, **r} for r in readings)

            # Generate maintenance records
            maint_list = MAINTENANCE_HISTORY.get(machine_def["name"], [])
            for days_ago, maint_type, failure_mode, part_used, desc in maint_list:
                maint_date = datetime.utcnow() - timedelta(days=days_ago)
                session.add(MaintenanceRecord(
                    machine_id=m.id,
                    maintenance_date=maint_date,
                    maintenance_type=maint_type,
                    failure_mode=failure_mode,
                    description=desc,
                    part_used=part_used,
                ))

            readings_count = len(readings)
            print(f"  [OK] {machine_def['name']}: {readings_count} readings | criticality={machine_def['criticality']} | scenario={FAILURE_SCENARIOS.get(machine_def['name'], ('None',))[0]}")

        # One Core insert for every sensor reading. SQLAlchemy batches the list into
        # multi-row INSERT statements, so this is a handful of round trips instead of
        # tens of thousands.
        if pending_readings:
            print(f"  ... inserting {len(pending_readings):,} sensor readings")
            session.execute(insert(SensorReading), pending_readings)

        session.commit()

    print(f"\n[DONE] Synthetic data generation complete!")
    print(f"   {len(MACHINES)} machines")
    print(f"   {len(MACHINES) * DAYS * READINGS_PER_DAY:,} sensor readings")
    print(f"   {len(FAILURE_SCENARIOS)} failure scenarios")
    print(f"   M-102 (DEMO): BEARING_DEGRADATION pattern -> high risk by day 80+")


if __name__ == "__main__":
    generate_and_seed()
