"""
Demo operator accounts.

Seeded at startup so the role model can be demonstrated without a signup flow.
The shared password comes from settings and is documented in the README — it is a
demo credential, not a secret.

Only the three usernames in `DEMO_USERS` are ever touched, so an account created
by any other means is never modified.
"""
import logging

from sqlalchemy import select

from app.config import get_settings
from app.database import AsyncSessionLocal
from app.models.auth import User
from app.services.auth_service import hash_password, verify_password

logger = logging.getLogger(__name__)
settings = get_settings()

DEMO_USERS = (
    ("planner", "Priya Nair", "planner"),
    ("technician", "Tom Alvarez", "technician"),
    ("viewer", "Read-Only Display", "viewer"),
)


async def seed_demo_users() -> int:
    """Create missing demo accounts and realign their password with settings.

    Returns how many accounts were created or updated.

    The realignment matters more than it looks. These accounts previously kept
    whatever password they were first seeded with, because an existing username was
    skipped outright. Two consequences, both observed against a real database:

    * Changing `DEMO_PASSWORD` on a deployment had no effect. The operator is then
      locked out of accounts whose password they believe they just set, with no
      error that explains why.
    * More seriously, an account seeded while the shipped default was in effect
      kept authenticating with that published default after the setting changed.
      `verify_startup_configuration` inspects the *setting*, but the stored hash is
      what actually authenticates, so the guard could pass while the documented
      default still opened the account.

    The password is only rewritten when it does not already match, so a steady-state
    boot performs no writes.
    """
    changed = 0
    try:
        async with AsyncSessionLocal() as db:
            for username, full_name, role in DEMO_USERS:
                existing = (
                    await db.execute(select(User).where(User.username == username))
                ).scalar_one_or_none()

                if existing is None:
                    salt, digest = hash_password(settings.demo_password)
                    db.add(
                        User(
                            username=username,
                            full_name=full_name,
                            role=role,
                            password_salt=salt,
                            password_hash=digest,
                        )
                    )
                    changed += 1
                    continue

                if not verify_password(
                    settings.demo_password,
                    existing.password_salt,
                    existing.password_hash,
                ):
                    existing.password_salt, existing.password_hash = hash_password(
                        settings.demo_password
                    )
                    changed += 1
                    logger.info(
                        f"Realigned demo account '{username}' with the current "
                        "DEMO_PASSWORD."
                    )

            if changed:
                await db.commit()
                logger.info(f"Seeded or updated {changed} demo user account(s)")
    except Exception as exc:  # pragma: no cover - defensive
        # Never block startup on user seeding.
        logger.error(f"Demo user seeding failed: {exc}")
    return changed
