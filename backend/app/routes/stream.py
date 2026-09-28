"""
Live event stream — GET /api/stream

Server-Sent Events rather than WebSockets: the data flows one way (server to
browser), SSE reconnects automatically, and it needs no extra dependency or
protocol upgrade. The existing 15-second poll stays in place as the fallback, so
a proxy that buffers SSE degrades to the old behaviour instead of going blank.

The stream carries a lightweight summary, not full payloads: it tells the client
that something changed and what the headline numbers are, and the client fetches
detail through the normal endpoints.
"""
import asyncio
import json
import logging
from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select

from app.database import AsyncSessionLocal
from app.models.alert import Alert
from app.models.sensor import SensorReading

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/stream", tags=["stream"])

POLL_SECONDS = 3
HEARTBEAT_EVERY = 10  # ticks without change before a keep-alive is sent


async def _snapshot() -> dict:
    """Cheap summary used to detect change without shipping the whole dashboard."""
    async with AsyncSessionLocal() as db:
        latest_reading = (
            await db.execute(select(func.max(SensorReading.timestamp)))
        ).scalar_one_or_none()

        severity_rows = (
            await db.execute(
                select(Alert.severity, func.count())
                .where(Alert.status.in_(("Active", "Acknowledged")))
                .group_by(Alert.severity)
            )
        ).all()

        max_risk = (
            await db.execute(
                select(func.max(Alert.risk_score)).where(
                    Alert.status.in_(("Active", "Acknowledged"))
                )
            )
        ).scalar_one_or_none()

    return {
        "latest_reading": latest_reading.isoformat() if latest_reading else None,
        "open_alerts": {sev or "Unknown": n for sev, n in severity_rows},
        "max_open_risk": round(max_risk, 1) if max_risk is not None else None,
    }


async def _event_generator(request: Request):
    last_payload = None
    idle_ticks = 0

    # Tell the client the current state immediately, so a late subscriber is not
    # blank until the next change.
    try:
        initial = await _snapshot()
        last_payload = json.dumps(initial, sort_keys=True)
        yield f"event: snapshot\ndata: {last_payload}\n\n"
    except Exception as exc:
        logger.warning(f"Stream initial snapshot failed: {exc}")

    while True:
        # Stop promptly when the browser navigates away, rather than holding a
        # database session open for a client that has gone.
        if await request.is_disconnected():
            break

        await asyncio.sleep(POLL_SECONDS)

        try:
            payload = json.dumps(await _snapshot(), sort_keys=True)
        except Exception as exc:
            logger.warning(f"Stream snapshot failed: {exc}")
            continue

        if payload != last_payload:
            last_payload = payload
            idle_ticks = 0
            yield f"event: update\ndata: {payload}\n\n"
        else:
            idle_ticks += 1
            if idle_ticks >= HEARTBEAT_EVERY:
                idle_ticks = 0
                # Comment frame: keeps intermediaries from closing an idle stream.
                yield f": heartbeat {datetime.utcnow().isoformat()}\n\n"


@router.get("")
async def stream(request: Request):
    """Subscribe to live plant updates over SSE."""
    return StreamingResponse(
        _event_generator(request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # Disable proxy buffering, which otherwise delays events until the
            # response closes and makes the stream look broken.
            "X-Accel-Buffering": "no",
        },
    )
