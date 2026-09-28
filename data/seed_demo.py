"""
Demo Seed Script — spec §48-49.
Ensures the M-102 demo scenario exists with correct high-risk state.
DEMO_MODE must always work without external dependencies.
"""
import sys
import os
# Add backend/ to path so 'app' package is importable
BACKEND_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'backend')
sys.path.insert(0, BACKEND_DIR)
os.chdir(BACKEND_DIR)  # Change to backend/ so relative DB path works

import json
from datetime import datetime, timedelta
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.models.machine import Machine
from app.models.alert import Alert
from app.models.work_order import WorkOrder
from app.models.oee import OeeSnapshot
from app.database import sync_database_url

# Derived from DATABASE_URL, so this seeds Postgres on a deployment and SQLite
# locally. `SEED_DATABASE_URL` overrides it for build-time use.
DB_URL = os.environ.get("SEED_DATABASE_URL") or sync_database_url()


def seed_demo():
    """Ensure M-102 demo scenario is fully seeded."""
    engine = create_engine(DB_URL)

    with Session(engine) as session:
        # Find M-102
        machine = session.query(Machine).filter(Machine.name == "M-102").first()
        if not machine:
            print("M-102 not found. Run generate_synthetic_data.py first.")
            return

        # Check if demo alert already exists
        existing_alert = session.query(Alert).filter(
            Alert.machine_id == machine.id,
            Alert.risk_score >= 80,
        ).first()

        if not existing_alert:
            # Create the demo alert for M-102
            sensor_deviations = json.dumps({"vibration_pct": 42.0, "temperature_pct": 18.0})
            top_sensors = json.dumps(["vibration", "temperature"])

            alert = Alert(
                machine_id=machine.id,
                risk_score=87.0,
                severity="Critical",
                maintenance_priority=96.0,
                top_sensors=top_sensors,
                sensor_deviations=sensor_deviations,
                failure_mode="BEARING_DEGRADATION",
                days_since_maintenance=63.0,
                previous_failure_context="Bearing-related failure recorded 125 days ago",
                machine_criticality="High",
                production_impact="High",
                part_needed="Bearing Assembly",
                part_available=True,
                recommended_action="Inspect and replace bearing assembly. Check lubrication.",
                status="Active",
                created_at=datetime.utcnow() - timedelta(hours=2),
                explanation_text=(
                    "Machine M-102 currently has an estimated 87% failure risk over the next 7 days. "
                    "Sensor analysis shows vibration is 42% above its recent baseline and temperature "
                    "is trending 18% above normal. This machine has not received maintenance in 63 days, "
                    "making it overdue for service. Historical records note a bearing-related failure "
                    "125 days ago. Recommended action: Inspect and replace bearing assembly."
                ),
                explanation_cached=True,
            )
            session.add(alert)
            session.flush()
            print(f"  [OK] Created demo alert for M-102 (ID: {alert.id})")

            # Seed OEE snapshots showing a simulated downtime penalty
            for hours_ago in range(24, 0, -1):
                # OEE dips as machine risk rises
                base_oee = 0.82
                penalty = min(0.15, (24 - hours_ago) * 0.006)
                oee_val = max(0.65, base_oee - penalty)
                avail = max(0.70, 0.88 - penalty)
                perf = 0.96
                qual = 0.97
                actual_oee = avail * perf * qual

                session.add(OeeSnapshot(
                    machine_id=machine.id,
                    timestamp=datetime.utcnow() - timedelta(hours=hours_ago),
                    availability=round(avail, 4),
                    performance=round(perf, 4),
                    quality=round(qual, 4),
                    oee=round(actual_oee, 4),
                ))

            session.commit()
            print("  [OK] Seeded OEE snapshots for M-102")
        else:
            print(f"  [INFO] Demo alert for M-102 already exists (ID: {existing_alert.id})")

        print("\n[DONE] Demo seed complete!")
        print("   M-102: 87% risk, BEARING_DEGRADATION, 63 days since maintenance")
        print("   Explain This Alert -> cached deterministic explanation ready")
        print("   Create Work Order -> Urgent, Technician B, Bearing Assembly, Next business day")


if __name__ == "__main__":
    seed_demo()
