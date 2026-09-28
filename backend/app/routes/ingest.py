"""
Ingestion API — POST /api/readings, POST /api/readings/csv,
GET /api/readings/status

The OT ingress point. The built-in simulator is just one producer writing through
the same path, which is what makes this a data platform rather than a closed
demo: a real line can POST readings or upload a CSV export and get scored
identically.
"""
import csv
import io
import logging
from datetime import datetime
from typing import List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Request, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from app.database import get_db
from app.models.machine import Machine
from app.models.sensor import SensorReading
from app.schemas import (
    IngestRequest,
    IngestResponse,
    IngestRowError,
    IngestStatusOut,
    IngestSourceStat,
    SensorReadingIn,
    MAX_INGEST_ROWS,
)
from app.services.live_feed import update_machine_risk
from app.services.rules_engine import classify_risk
from app.services.baseline_service import refresh_machine_baseline
from app.services.auth_service import require_permission, record_audit
from app.security import read_capped_body

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/readings", tags=["readings"])

# Columns a CSV must provide; the rest fall back to SensorReadingIn defaults.
CSV_REQUIRED = ("vibration", "temperature", "rpm")


async def _resolve_machine(
    db: AsyncSession,
    machine_id: Optional[int],
    machine_name: Optional[str],
) -> Machine:
    """Look up the target machine by id or name, or fail with a clear code."""
    if machine_id is None and not machine_name:
        raise HTTPException(status_code=400, detail="MACHINE_NOT_SPECIFIED")

    query = select(Machine)
    if machine_id is not None:
        query = query.where(Machine.id == machine_id)
    else:
        query = query.where(Machine.name == machine_name)

    machine = (await db.execute(query)).scalar_one_or_none()
    if machine is None:
        raise HTTPException(status_code=404, detail="MACHINE_NOT_FOUND")
    return machine


async def _persist_and_score(
    db: AsyncSession,
    machine: Machine,
    readings: List[SensorReadingIn],
    source: str,
) -> Tuple[SensorReading, int]:
    """Insert readings then re-score the machine through the normal path."""
    last_row = None
    accepted = 0
    for item in readings:
        row = SensorReading(
            machine_id=machine.id,
            timestamp=item.timestamp or datetime.utcnow(),
            vibration=item.vibration,
            temperature=item.temperature,
            rpm=item.rpm,
            machine_status=item.machine_status,
            production_count=item.production_count,
            good_count=item.good_count,
            planned_production_time=item.planned_production_time,
            actual_run_time=item.actual_run_time,
            downtime_minutes=item.downtime_minutes,
            source=source,
        )
        db.add(row)
        last_row = row
        accepted += 1

    await db.commit()
    if last_row is not None:
        await db.refresh(last_row)
        # Refresh the observed baseline now that new history has landed. Baselines
        # move slowly, so this belongs at ingest time rather than on every score.
        await refresh_machine_baseline(db, machine)
        # Same scoring path the simulator uses — ingested data is not a
        # second-class citizen.
        await update_machine_risk(db, machine, last_row)
        await db.refresh(last_row)

    return last_row, accepted


@router.post("", response_model=IngestResponse, status_code=201)
async def ingest_readings(
    payload: IngestRequest,
    db: AsyncSession = Depends(get_db),
    actor: dict = Depends(require_permission("reading:ingest")),
):
    """Ingest a batch of OT sensor readings and immediately re-score the machine."""
    machine = await _resolve_machine(db, payload.machine_id, payload.machine_name)

    last_row, accepted = await _persist_and_score(db, machine, payload.readings, "api")
    await record_audit(
        db, actor, "READINGS_INGESTED", "machine", machine.id,
        f"{accepted} reading(s) via api",
    )
    await db.commit()

    risk = last_row.risk_score if last_row is not None else None
    return IngestResponse(
        machine_id=machine.id,
        machine_name=machine.name,
        accepted=accepted,
        rejected=0,
        errors=[],
        risk_score=risk,
        severity=classify_risk(risk) if risk is not None else None,
        source="api",
    )


@router.post("/csv", response_model=IngestResponse, status_code=201)
async def ingest_csv(
    request: Request,
    machine_name: str = Query(..., description="Target machine name, e.g. M-102"),
    db: AsyncSession = Depends(get_db),
    actor: dict = Depends(require_permission("reading:ingest")),
):
    """Ingest readings from a CSV export sent as the raw request body.

    Takes the CSV as `text/csv` rather than a multipart upload so no extra
    dependency is needed; the browser reads the file and posts its text.

    Bad rows are reported individually rather than failing the whole upload —
    a plant export with three malformed lines should still load.
    """
    machine = await _resolve_machine(db, None, machine_name)

    # Size-capped: `request.body()` buffers the whole payload in memory, so an
    # unbounded read lets one request exhaust the process.
    raw = await read_capped_body(request)
    if not raw:
        raise HTTPException(status_code=400, detail="CSV_EMPTY")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="FILE_NOT_UTF8")

    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise HTTPException(status_code=400, detail="CSV_EMPTY")

    missing = [c for c in CSV_REQUIRED if c not in reader.fieldnames]
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"CSV_MISSING_COLUMNS:{','.join(missing)}",
        )

    parsed: List[SensorReadingIn] = []
    errors: List[IngestRowError] = []

    for line_no, row in enumerate(reader, start=2):  # line 1 is the header
        if len(parsed) >= MAX_INGEST_ROWS:
            errors.append(IngestRowError(row=line_no, error="ROW_LIMIT_EXCEEDED"))
            break
        try:
            cleaned = {k: v for k, v in row.items() if v not in (None, "")}
            parsed.append(SensorReadingIn(**cleaned))
        except Exception as exc:
            errors.append(IngestRowError(row=line_no, error=str(exc).split("\n")[0][:200]))

    if not parsed:
        raise HTTPException(status_code=400, detail="CSV_NO_VALID_ROWS")

    last_row, accepted = await _persist_and_score(db, machine, parsed, "csv")
    await record_audit(
        db, actor, "READINGS_INGESTED", "machine", machine.id,
        f"{accepted} accepted, {len(errors)} rejected via csv",
    )
    await db.commit()

    risk = last_row.risk_score if last_row is not None else None
    return IngestResponse(
        machine_id=machine.id,
        machine_name=machine.name,
        accepted=accepted,
        rejected=len(errors),
        errors=errors,
        risk_score=risk,
        severity=classify_risk(risk) if risk is not None else None,
        source="csv",
    )


@router.get("/status", response_model=IngestStatusOut)
async def ingest_status(db: AsyncSession = Depends(get_db)):
    """Reading counts and recency grouped by provenance."""
    result = await db.execute(
        select(
            SensorReading.source,
            func.count(SensorReading.id),
            func.max(SensorReading.timestamp),
        ).group_by(SensorReading.source)
    )
    rows = result.all()

    stats = [
        IngestSourceStat(
            source=source or "unknown",
            readings=count,
            latest_timestamp=latest,
        )
        for source, count, latest in rows
    ]
    stats.sort(key=lambda s: s.readings, reverse=True)

    return IngestStatusOut(
        total_readings=sum(s.readings for s in stats),
        by_source=stats,
    )
