from sqlalchemy import Column, Integer, String, DateTime, Boolean
from app.database import Base
from datetime import datetime


class User(Base):
    """Operator account with a role.

    Passwords are stored as PBKDF2-HMAC-SHA256 hashes with a per-user salt, using
    only the standard library — no new dependency for something this small.

    Accounts arrive two ways: seeded demo operators (`planner`, `technician`,
    `viewer`), which have no email, and self-registered accounts, which log in with
    their email address. `username` remains the identity the token and audit log are
    keyed on, so a registered account's username is its email.
    """

    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, nullable=False)
    # Nullable because the seeded demo accounts predate registration and have no
    # address. Unique so one address cannot hold two accounts.
    email = Column(String, unique=True, nullable=True, index=True)
    # NULL is read as "not verified". Nullable rather than `default=False` so the
    # column can be added to an existing table on either dialect without a
    # dialect-specific DEFAULT clause.
    email_verified = Column(Boolean, nullable=True, default=False)
    full_name = Column(String, nullable=True)
    # Role gates mutating actions: planner can raise/close work, technician can
    # progress assigned work, viewer is read-only.
    role = Column(String, nullable=False, default="viewer")  # planner, technician, viewer
    password_salt = Column(String, nullable=False)
    password_hash = Column(String, nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class AuditLog(Base):
    """Immutable record of who changed what.

    Without this, an alert acknowledgement or a completed work order has no
    accountable owner, which is the first thing an auditor asks about.
    """

    __tablename__ = "audit_log"

    id = Column(Integer, primary_key=True, index=True)
    actor = Column(String, nullable=False)
    role = Column(String, nullable=True)
    action = Column(String, nullable=False)          # e.g. ALERT_ACKNOWLEDGED
    entity_type = Column(String, nullable=False)     # alert, work_order, reading
    entity_id = Column(Integer, nullable=True)
    details = Column(String, nullable=True)
    timestamp = Column(DateTime, nullable=False, default=datetime.utcnow)


class NotificationLog(Base):
    """Delivery attempts for Critical-alert notifications.

    Recorded so delivery is verifiable rather than asserted; a webhook that
    silently fails is worse than no webhook.
    """

    __tablename__ = "notification_log"

    id = Column(Integer, primary_key=True, index=True)
    alert_id = Column(Integer, nullable=True)
    channel = Column(String, nullable=False, default="webhook")
    target = Column(String, nullable=True)
    status = Column(String, nullable=False)           # sent, failed, skipped
    detail = Column(String, nullable=True)
    timestamp = Column(DateTime, nullable=False, default=datetime.utcnow)
