"""Outbound email for account verification.

Standard library only (`smtplib`, `email.message`), consistent with the rest of the
auth stack: no new dependency for one message type.

The important behaviour is the fallback. With no SMTP host configured nothing is
sent, and `send_verification_email` reports that plainly so the caller can return
the link in the API response instead. The alternative — pretending a message was
delivered — would leave a new account unable to sign in with no way to find out why,
which is the worst of the three outcomes.

Delivery never blocks registration. A refused relay should not lose an account that
has already been created, so failures are logged and reported, not raised.
"""
import logging
import smtplib
import ssl
from email.message import EmailMessage
from typing import Optional, Tuple

from app.config import get_settings

logger = logging.getLogger(__name__)


def smtp_configured() -> bool:
    settings = get_settings()
    return bool(settings.smtp_host and settings.smtp_from)


def verification_link(token: str) -> str:
    """Absolute URL a person clicks to verify.

    Points at the FRONTEND, which owns the /verify route and can show a result and a
    sign-in prompt. Pointing it at the API would return raw JSON to someone who
    opened it in a browser.
    """
    settings = get_settings()
    base = (settings.frontend_base_url or "").strip().rstrip("/")
    if not base:
        # The deployed frontend is, by definition, an allowed CORS origin.
        origins = [o for o in settings.allowed_origins if o.startswith("http")]
        base = (origins[0] if origins else "http://localhost:5173").rstrip("/")
    return f"{base}/verify?token={token}"


def _build_message(to_email: str, link: str) -> EmailMessage:
    settings = get_settings()
    message = EmailMessage()
    message["Subject"] = "Confirm your PredictOps account"
    message["From"] = settings.smtp_from
    message["To"] = to_email
    message.set_content(
        "Confirm your PredictOps account\n\n"
        "Open this link to finish creating your account:\n\n"
        f"{link}\n\n"
        "The link expires in 24 hours. If you did not request an account, ignore "
        "this message — nothing was created that can be used without it.\n"
    )
    return message


def send_verification_email(to_email: str, token: str) -> Tuple[bool, Optional[str]]:
    """Return `(sent, detail)`.

    `sent` is False both when SMTP is not configured and when delivery failed; the
    detail says which. Never raises.
    """
    link = verification_link(token)

    if not smtp_configured():
        logger.info(
            "SMTP not configured; verification link returned to the caller instead "
            "of being emailed."
        )
        return False, "smtp_not_configured"

    settings = get_settings()
    try:
        message = _build_message(to_email, link)
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as smtp:
            if settings.smtp_use_tls:
                smtp.starttls(context=ssl.create_default_context())
            if settings.smtp_username:
                smtp.login(settings.smtp_username, settings.smtp_password)
            smtp.send_message(message)
        logger.info(f"Verification email sent to {to_email}")
        return True, None
    except Exception as exc:
        # Logged with the address but never the password, and the reason is short:
        # an SMTP traceback in a response body tells an attacker about the relay.
        logger.warning(f"Verification email to {to_email} failed: {str(exc)[:200]}")
        return False, "delivery_failed"
