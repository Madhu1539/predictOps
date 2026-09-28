"""First-boot seeding of the demo fleet.

A deployed instance starts against an empty database. `init_db` creates the
schema but no rows, so without this the Command Center renders an empty fleet and
the Investigation tab correctly reports that there is nothing on record — which
looks like a broken deployment rather than an unseeded one.

Why this wraps the existing synchronous scripts instead of reimplementing them:
`data/generate_synthetic_data.py` is the single source of truth for the fleet, its
sensor history and the failure scenarios, and it shares `FAILURE_SCENARIOS` with
`app.ml.labeling` so generated data and training labels cannot drift. Rewriting
that generation logic asynchronously would risk changing the distribution the
model card documents. The scripts are already idempotent — each returns early when
machines exist — so they are safe to invoke on every boot.

Runs off the event loop in a worker thread, and is started as a background task
rather than awaited, because seeding inserts roughly 43,000 sensor readings
(20 machines x 90 days x 24 hourly readings) through the ORM. Against network
Postgres that takes long enough to trip a platform health check if it blocked
startup.
"""
import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Optional

from sqlalchemy import func, select

from app.database import AsyncSessionLocal
from app.models.machine import Machine

logger = logging.getLogger(__name__)

# State for /api/health, so an operator can tell "still seeding" from "seeding
# failed" from "nothing to do" without reading the logs.
_status: str = "not_started"
_detail: Optional[str] = None

REPO_ROOT = Path(__file__).resolve().parents[3]
DATA_DIR = REPO_ROOT / "data"


def seed_status() -> dict:
    return {"status": _status, "detail": _detail}


async def _machine_count() -> int:
    async with AsyncSessionLocal() as db:
        return (await db.execute(select(func.count()).select_from(Machine))).scalar() or 0


def _run_sync_seeders() -> str:
    """Execute the generator and the demo-scenario seeder in this thread.

    Imported here rather than at module scope: `data/` is not a package and the
    scripts mutate `sys.path` and the working directory at import time.
    """
    if not DATA_DIR.is_dir():
        raise FileNotFoundError(f"data directory not found at {DATA_DIR}")

    # The scripts chdir to backend/ so relative SQLite paths resolve. Restore it
    # afterwards or every later relative path in the process resolves differently.
    original_cwd = os.getcwd()
    if str(DATA_DIR) not in sys.path:
        sys.path.insert(0, str(DATA_DIR))
    try:
        import generate_synthetic_data

        generate_synthetic_data.generate_and_seed()

        import seed_demo

        seed_demo.seed_demo()
        return "generated fleet and demo scenario"
    finally:
        os.chdir(original_cwd)


async def seed_demo_fleet_if_empty() -> dict:
    """Seed the demo fleet when the database holds no machines.

    Never raises: a failure here must not take down an otherwise working API. The
    reason is recorded and surfaced through /api/health.
    """
    global _status, _detail

    try:
        existing = await _machine_count()
    except Exception as exc:
        _status, _detail = "failed", f"could not count machines: {str(exc)[:200]}"
        logger.warning(f"Demo seed skipped: {_detail}")
        return seed_status()

    if existing > 0:
        _status, _detail = "skipped", f"{existing} machines already present"
        logger.info(f"Demo seed not needed: {_detail}")
        return seed_status()

    _status, _detail = "running", "generating synthetic fleet"
    logger.info("Empty database detected; seeding demo fleet in the background...")

    try:
        detail = await asyncio.to_thread(_run_sync_seeders)
        count = await _machine_count()
        _status, _detail = "completed", f"{detail}; {count} machines"
        logger.info(f"Demo seed complete: {_detail}")
    except Exception as exc:
        _status, _detail = "failed", str(exc)[:300]
        logger.error(f"Demo seed failed: {_detail}", exc_info=True)

    return seed_status()
