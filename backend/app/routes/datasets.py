"""
Datasets API — POST/GET/DELETE /api/datasets

A dataset groups machines and readings that arrived from one source, so an
uploaded factory can be reported on and then removed without touching the demo
fleet. The demo fleet is `dataset_id IS NULL` and is deliberately not deletable
through this route.
"""
import io
import json
import logging
from typing import Dict, List, Tuple

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.alert import Alert
from app.models.dataset import Dataset
from app.models.machine import Machine
from app.models.oee import OeeSnapshot
from app.models.sensor import SensorReading
from app.models.work_order import WorkOrder
from app.routes.ingest import _persist_and_score
from app.schemas import (
    ColumnMappingPreview,
    DatasetCreate,
    DatasetDeleteOut,
    DatasetOut,
    IngestRowError,
    SensorReadingIn,
    UploadResult,
    MAX_INGEST_ROWS,
)
from app.services.auth_service import record_audit, require_permission
from app.security import read_capped_body
from app.services.column_mapping import (
    detect_mapping,
    fahrenheit_to_celsius,
    looks_like_fahrenheit,
    parse_timestamp,
)
from app.services.dataset_service import get_or_create_machine
from app.services.data_quality import assess_dataset_quality

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/datasets", tags=["datasets"])

# Rows shown back to the user during preview.
PREVIEW_ROWS = 5
# Guard against a pivoted or malformed file producing thousands of "machines".
MAX_MACHINES_PER_UPLOAD = 200


async def _read_csv(request: Request) -> Tuple[pd.DataFrame, List[str]]:
    """Read the request body as CSV. Shared by preview and upload.

    Size-capped: `pandas.read_csv` expands a body well beyond its wire size, so an
    unbounded read here is a memory-exhaustion lever on a single request.
    """
    raw = await read_capped_body(request)
    if not raw:
        raise HTTPException(status_code=400, detail="CSV_EMPTY")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="FILE_NOT_UTF8")

    try:
        # `keep_default_na` left on so blanks become NaN and are handled per row.
        frame = pd.read_csv(io.StringIO(text))
    except Exception as exc:
        # The parser's message can echo file contents and local paths back to the
        # caller; log it and return a stable code instead.
        logger.info("CSV parse failed: %s", str(exc)[:200])
        raise HTTPException(status_code=400, detail="CSV_UNPARSEABLE")

    if frame.empty or not list(frame.columns):
        raise HTTPException(status_code=400, detail="CSV_EMPTY")
    return frame, [str(c) for c in frame.columns]


@router.post("", response_model=DatasetOut, status_code=201)
async def create_dataset(
    payload: DatasetCreate,
    db: AsyncSession = Depends(get_db),
    actor: dict = Depends(require_permission("reading:ingest")),
):
    """Create an empty dataset, ready to receive readings."""
    dataset = Dataset(
        name=payload.name,
        description=payload.description,
        source=payload.source,
        status="draft",
    )
    db.add(dataset)
    await db.commit()
    await db.refresh(dataset)

    await record_audit(db, actor, "DATASET_CREATED", "dataset", dataset.id, payload.name)
    await db.commit()

    return DatasetOut.model_validate(dataset)


@router.get("", response_model=List[DatasetOut])
async def list_datasets(db: AsyncSession = Depends(get_db)):
    """All uploaded datasets, newest first. The demo fleet is not listed here
    because it is not a dataset row."""
    rows = (
        await db.execute(select(Dataset).order_by(Dataset.created_at.desc()))
    ).scalars().all()
    return [DatasetOut.model_validate(d) for d in rows]


@router.get("/{dataset_id}", response_model=DatasetOut)
async def get_dataset(dataset_id: int, db: AsyncSession = Depends(get_db)):
    dataset = (
        await db.execute(select(Dataset).where(Dataset.id == dataset_id))
    ).scalar_one_or_none()
    if dataset is None:
        raise HTTPException(status_code=404, detail="DATASET_NOT_FOUND")
    return DatasetOut.model_validate(dataset)


@router.post("/{dataset_id}/preview", response_model=ColumnMappingPreview)
async def preview_upload(
    dataset_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: dict = Depends(require_permission("reading:ingest")),
):
    """Interpret a CSV without writing anything.

    Deliberately read-only: the user confirms how their headers were understood
    before any machine or reading is created. Sent as `text/csv` in the request
    body, matching the existing ingestion endpoint.

    Gated on the same capability as the upload it precedes. It writes nothing, but
    it does run the CSV parser and echo file contents back, so leaving it open made
    the permission on `/upload` avoidable for anything short of persistence.
    """
    dataset = (
        await db.execute(select(Dataset).where(Dataset.id == dataset_id))
    ).scalar_one_or_none()
    if dataset is None:
        raise HTTPException(status_code=404, detail="DATASET_NOT_FOUND")

    frame, headers = await _read_csv(request)
    detected = detect_mapping(headers)
    mapping = detected["mapping"]

    notes: List[str] = []
    sample_rows: List[dict] = []
    detected_machines: List[str] = []
    timestamp_failures = 0
    fahrenheit = False

    preview_frame = frame.head(PREVIEW_ROWS)

    if "machine" in mapping:
        values = frame[mapping["machine"]].dropna().astype(str).str.strip()
        detected_machines = sorted({v for v in values if v})[:MAX_MACHINES_PER_UPLOAD]
    else:
        notes.append(
            "No machine column detected. All readings will be attached to a single "
            "machine named after the dataset."
        )

    if "timestamp" in mapping:
        parsed = preview_frame[mapping["timestamp"]].map(parse_timestamp)
        timestamp_failures = int(parsed.isna().sum())
        if timestamp_failures:
            notes.append(
                f"{timestamp_failures} of the first {len(preview_frame)} timestamps "
                "could not be parsed and those rows will be rejected."
            )
    else:
        notes.append("No timestamp column detected; ingestion time will be used instead.")

    if "temperature" in mapping:
        fahrenheit = looks_like_fahrenheit(
            pd.to_numeric(frame[mapping["temperature"]], errors="coerce").dropna().tolist(),
            header=mapping["temperature"],
        )
        if fahrenheit:
            notes.append(
                "Temperatures look like Fahrenheit. Re-send with convert_fahrenheit=true "
                "to convert, or leave as-is if the unit is correct."
            )

    for _, row in preview_frame.iterrows():
        sample_rows.append({
            field: (None if pd.isna(row[column]) else str(row[column])[:40])
            for field, column in mapping.items()
        })

    if detected["missing_required"]:
        notes.append(
            "No sensor columns found. At least one of vibration, temperature or rpm "
            "is needed. Supply one or rename the column so it can be recognised."
        )
    elif detected["missing_sensors"]:
        missing = ", ".join(detected["missing_sensors"])
        notes.append(
            f"No {missing} column. This is accepted: the deviation score and the "
            "absolute published limits will be computed from the channels you do "
            "have. The ML risk score needs all three, so it will be reported as "
            "unavailable rather than guessed."
        )

    return ColumnMappingPreview(
        headers=headers,
        mapping=mapping,
        confidence=detected["confidence"],
        unmapped_headers=detected["unmapped_headers"],
        missing_required=detected["missing_required"],
        present_sensors=detected["present_sensors"],
        missing_sensors=detected["missing_sensors"],
        ml_scoreable=detected["ml_scoreable"],
        detected_machines=detected_machines,
        sample_rows=sample_rows,
        total_rows_previewed=len(preview_frame),
        timestamp_parse_failures=timestamp_failures,
        temperature_looks_fahrenheit=fahrenheit,
        ready_to_commit=not detected["missing_required"],
        notes=notes,
    )


@router.post("/{dataset_id}/upload", response_model=UploadResult)
async def upload_readings(
    dataset_id: int,
    request: Request,
    convert_fahrenheit: bool = Query(False),
    machine_type: str = Query("Unknown"),
    db: AsyncSession = Depends(get_db),
    actor: dict = Depends(require_permission("reading:ingest")),
):
    """Commit a CSV into a dataset, provisioning machines as needed.

    Rows are validated individually so a partially malformed export still loads,
    and every machine created here is `external`, which keeps the simulator away
    from it.
    """
    dataset = (
        await db.execute(select(Dataset).where(Dataset.id == dataset_id))
    ).scalar_one_or_none()
    if dataset is None:
        raise HTTPException(status_code=404, detail="DATASET_NOT_FOUND")

    frame, headers = await _read_csv(request)
    detected = detect_mapping(headers)
    mapping = detected["mapping"]
    if detected["missing_required"]:
        raise HTTPException(
            status_code=400,
            detail="CSV_NO_SENSOR_COLUMNS:need at least one of "
                   + ",".join(detected["missing_required"]),
        )

    grouped: Dict[str, List[dict]] = {}
    errors: List[IngestRowError] = []
    accepted = 0

    for offset, (_, row) in enumerate(frame.iterrows(), start=2):  # line 1 = header
        if accepted >= MAX_INGEST_ROWS:
            errors.append(IngestRowError(row=offset, error="ROW_LIMIT_EXCEEDED"))
            break
        try:
            machine_label = (
                str(row[mapping["machine"]]).strip()
                if "machine" in mapping and not pd.isna(row[mapping["machine"]])
                else dataset.name
            )
            if not machine_label:
                raise ValueError("blank machine identifier")

            timestamp = None
            if "timestamp" in mapping:
                timestamp = parse_timestamp(row[mapping["timestamp"]])
                if timestamp is None:
                    raise ValueError("unparseable timestamp")

            temperature = None
            if "temperature" in mapping and not pd.isna(row[mapping["temperature"]]):
                temperature = float(row[mapping["temperature"]])
                if convert_fahrenheit:
                    temperature = fahrenheit_to_celsius(temperature)

            def optional_sensor(field: str):
                """Absent channels stay absent. Substituting a value here would be
                indistinguishable from a real measurement downstream."""
                if field not in mapping or pd.isna(row[mapping[field]]):
                    return None
                return float(row[mapping[field]])

            payload = {
                "timestamp": timestamp,
                "vibration": optional_sensor("vibration"),
                "temperature": temperature,
                "rpm": optional_sensor("rpm"),
            }
            for field, default in (
                ("machine_status", "Running"),
                ("production_count", 0),
                ("good_count", 0),
                ("planned_production_time", 60.0),
                ("actual_run_time", 60.0),
                ("downtime_minutes", 0.0),
            ):
                if field in mapping and not pd.isna(row[mapping[field]]):
                    payload[field] = row[mapping[field]]
                else:
                    payload[field] = default

            validated = SensorReadingIn(**payload)
            grouped.setdefault(machine_label, []).append(validated)
            accepted += 1
        except Exception as exc:
            errors.append(
                IngestRowError(row=offset, error=str(exc).split("\n")[0][:200])
            )

    if not grouped:
        raise HTTPException(status_code=400, detail="CSV_NO_VALID_ROWS")
    if len(grouped) > MAX_MACHINES_PER_UPLOAD:
        raise HTTPException(
            status_code=400,
            detail=f"TOO_MANY_MACHINES:{len(grouped)}>{MAX_MACHINES_PER_UPLOAD}",
        )

    created = matched = 0
    names: List[str] = []
    for label, readings in grouped.items():
        machine, was_created = await get_or_create_machine(
            db, label, dataset_id, machine_type=machine_type
        )
        created += int(was_created)
        matched += int(not was_created)
        names.append(label)
        await _persist_and_score(db, machine, readings, "csv")

    dataset.status = "ready"
    dataset.row_count = (dataset.row_count or 0) + accepted
    dataset.machine_count = created + matched
    dataset.column_mapping = json.dumps(mapping)
    await db.commit()

    await record_audit(
        db, actor, "DATASET_UPLOADED", "dataset", dataset_id,
        f"{accepted} readings across {len(grouped)} machine(s), {len(errors)} rejected",
    )
    await db.commit()

    return UploadResult(
        dataset_id=dataset_id,
        machines_created=created,
        machines_matched=matched,
        readings_accepted=accepted,
        readings_rejected=len(errors),
        errors=errors[:50],
        machines=sorted(names),
    )


@router.get("/{dataset_id}/quality")
async def dataset_quality(dataset_id: int, db: AsyncSession = Depends(get_db)):
    """Data quality report for a dataset.

    Pass `0` to assess the built-in demo fleet. The same checks run either way,
    which is what makes them demonstrable rather than a claim.
    """
    target = None if dataset_id == 0 else dataset_id
    if target is not None:
        dataset = (
            await db.execute(select(Dataset).where(Dataset.id == target))
        ).scalar_one_or_none()
        if dataset is None:
            raise HTTPException(status_code=404, detail="DATASET_NOT_FOUND")

    return await assess_dataset_quality(db, target)


@router.delete("/{dataset_id}", response_model=DatasetDeleteOut)
async def delete_dataset(
    dataset_id: int,
    db: AsyncSession = Depends(get_db),
    actor: dict = Depends(require_permission("reading:ingest")),
):
    """Remove a dataset and everything derived from it.

    Scoped strictly to machines carrying this `dataset_id`, so the demo fleet
    (`dataset_id IS NULL`) can never be caught by this deletion.
    """
    dataset = (
        await db.execute(select(Dataset).where(Dataset.id == dataset_id))
    ).scalar_one_or_none()
    if dataset is None:
        raise HTTPException(status_code=404, detail="DATASET_NOT_FOUND")

    machine_ids = [
        row[0]
        for row in (
            await db.execute(
                select(Machine.id).where(Machine.dataset_id == dataset_id)
            )
        ).all()
    ]

    readings = alerts = snapshots = 0
    if machine_ids:
        readings = (
            await db.execute(
                select(func.count()).select_from(SensorReading)
                .where(SensorReading.machine_id.in_(machine_ids))
            )
        ).scalar_one()
        alerts = (
            await db.execute(
                select(func.count()).select_from(Alert)
                .where(Alert.machine_id.in_(machine_ids))
            )
        ).scalar_one()
        snapshots = (
            await db.execute(
                select(func.count()).select_from(OeeSnapshot)
                .where(OeeSnapshot.machine_id.in_(machine_ids))
            )
        ).scalar_one()

        # Work orders reference alerts, so remove them before the alerts go.
        await db.execute(delete(WorkOrder).where(WorkOrder.machine_id.in_(machine_ids)))
        await db.execute(delete(OeeSnapshot).where(OeeSnapshot.machine_id.in_(machine_ids)))
        await db.execute(delete(Alert).where(Alert.machine_id.in_(machine_ids)))
        await db.execute(delete(SensorReading).where(SensorReading.machine_id.in_(machine_ids)))
        await db.execute(delete(Machine).where(Machine.id.in_(machine_ids)))

    await db.execute(delete(Dataset).where(Dataset.id == dataset_id))
    await record_audit(
        db, actor, "DATASET_DELETED", "dataset", dataset_id,
        f"{len(machine_ids)} machines, {readings} readings",
    )
    await db.commit()

    return DatasetDeleteOut(
        dataset_id=dataset_id,
        machines_deleted=len(machine_ids),
        readings_deleted=readings,
        alerts_deleted=alerts,
        oee_snapshots_deleted=snapshots,
    )
