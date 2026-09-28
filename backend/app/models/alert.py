from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey, Boolean, Index
from app.database import Base
from datetime import datetime


class Alert(Base):
    __tablename__ = "alerts"

    id = Column(Integer, primary_key=True, index=True)
    machine_id = Column(Integer, ForeignKey("machines.id"), nullable=False)
    # Nullable because the ML model needs all three sensor channels: a machine that
    # reports only temperature has no model score, and recording that honestly is
    # better than substituting the deviation score, which would look like a
    # prediction. `deviation_score` and `confidence_detail` carry the basis instead.
    risk_score = Column(Float, nullable=True)
    severity = Column(String, nullable=False)              # Normal, Watch, Warning, Critical
    maintenance_priority = Column(Float, nullable=False, default=0.0)
    top_sensors = Column(String, nullable=True)            # JSON string of top sensor deviations
    sensor_deviations = Column(String, nullable=True)      # JSON string
    failure_mode = Column(String, nullable=True)
    days_since_maintenance = Column(Float, nullable=True)
    previous_failure_context = Column(String, nullable=True)
    machine_criticality = Column(String, nullable=True)
    production_impact = Column(String, nullable=True)
    part_needed = Column(String, nullable=True)
    part_available = Column(Boolean, nullable=True, default=True)
    recommended_action = Column(String, nullable=True)
    status = Column(String, nullable=False, default="Active")  # Active, Acknowledged, Dismissed
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    explanation_text = Column(String, nullable=True)
    explanation_cached = Column(Boolean, default=False)
    # True when the risk score came from the rules-based fallback rather than
    # the ML model, so clients can show a degraded-mode indicator (spec §47).
    degraded_mode = Column(Boolean, nullable=False, default=False)
    # Modelled business impact (see cost_service). Nullable because alerts
    # raised before the cost model existed have no value, and because a machine
    # with no ERP cost centre falls back rather than fabricating a figure.
    estimated_downtime_cost = Column(Float, nullable=True)
    estimated_loss_avoided = Column(Float, nullable=True)
    # Top per-feature model contributions as JSON, so the UI can show why the
    # model scored this machine rather than a hand-written rationale.
    attribution = Column(String, nullable=True)
    # Model-free deviation from this machine's own reference, and how much the ML
    # score can be relied on. Stored so a report can show both signals side by
    # side rather than presenting one number as unqualified truth.
    deviation_score = Column(Float, nullable=True)
    deviation_detail = Column(String, nullable=True)   # JSON components
    confidence = Column(String, nullable=True)         # high | medium | low
    confidence_detail = Column(String, nullable=True)  # JSON reasons + basis

    __table_args__ = (
        Index("ix_alerts_machine_status", "machine_id", "status"),
    )
