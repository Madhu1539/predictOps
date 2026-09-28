"""
Critical-alert notification — fail-soft webhook delivery with a log.

Notification must never be load-bearing: if the webhook is down, scoring and
alerting continue and the failure is recorded. An unconfigured webhook is logged
as "skipped" rather than passing silently, so "no notifications" is
distinguishable from "notifications never attempted".
"""
import logging
from typing import Optional

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.auth import NotificationLog
from app.security import validate_webhook_url

logger = logging.getLogger(__name__)
settings = get_settings()

REQUEST_TIMEOUT_SECONDS = 5.0


async def notify_critical_alert(
    db: AsyncSession,
    alert_id: int,
    machine_name: str,
    risk_score: float,
    failure_mode: Optional[str],
    estimated_loss_avoided: Optional[float] = None,
) -> str:
    """POST a Critical alert to the configured webhook. Returns the status string."""
    configured = settings.notification_webhook_url

    if not configured:
        await _log(db, alert_id, configured, "skipped", "No webhook configured")
        return "skipped"

    # Validated before use: an unchecked configuration value turns this into a
    # request the server makes on someone else's behalf, and a non-http scheme
    # such as file:// is a local read rather than a notification.
    target = validate_webhook_url(configured)
    if target is None:
        await _log(db, alert_id, None, "skipped", "Webhook URL rejected as unsafe")
        return "skipped"

    payload = {
        "event": "critical_alert",
        "alert_id": alert_id,
        "machine": machine_name,
        "risk_score": risk_score,
        "failure_mode": failure_mode,
        "estimated_loss_avoided": estimated_loss_avoided,
        "text": (
            f"CRITICAL: {machine_name} at {risk_score:.0f}% failure risk"
            + (f" ({failure_mode.replace('_', ' ').lower()})" if failure_mode else "")
        ),
    }

    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            response = await client.post(target, json=payload)
        if response.status_code < 300:
            await _log(db, alert_id, target, "sent", f"HTTP {response.status_code}")
            return "sent"
        await _log(db, alert_id, target, "failed", f"HTTP {response.status_code}")
        return "failed"
    except Exception as exc:
        # Delivery failure is recorded, never raised: an unreachable webhook must
        # not stop the alert from being created.
        logger.warning(f"Notification for alert {alert_id} failed: {exc}")
        await _log(db, alert_id, target, "failed", str(exc)[:200])
        return "failed"


async def _log(
    db: AsyncSession,
    alert_id: Optional[int],
    target: Optional[str],
    status: str,
    detail: Optional[str],
) -> None:
    try:
        db.add(
            NotificationLog(
                alert_id=alert_id,
                channel="webhook",
                target=target or None,
                status=status,
                detail=detail,
            )
        )
        await db.flush()
    except Exception as exc:  # pragma: no cover - defensive
        logger.error(f"Failed to write notification log: {exc}")


async def recent_notifications(db: AsyncSession, limit: int = 20):
    result = await db.execute(
        select(NotificationLog).order_by(NotificationLog.timestamp.desc()).limit(limit)
    )
    return result.scalars().all()
