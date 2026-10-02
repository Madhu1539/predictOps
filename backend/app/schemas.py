from pydantic import BaseModel, Field, model_validator
from typing import Optional, List, Any
from datetime import datetime

# Upper bound on a single ingest batch, so one request cannot exhaust memory.
MAX_INGEST_ROWS = 5000


# ─── Machine Schemas ──────────────────────────────────────────────────────────

class MachineBase(BaseModel):
    name: str
    type: str
    location: str
    install_date: str
    criticality: str
    nominal_vibration: float
    nominal_temperature: float
    nominal_rpm: float


class MachineListItem(BaseModel):
    id: int
    name: str
    type: str
    location: str
    criticality: str
    latest_risk: Optional[float] = None
    severity: Optional[str] = "Normal"
    maintenance_priority: Optional[float] = None

    class Config:
        from_attributes = True


class SensorReadingOut(BaseModel):
    id: int
    machine_id: int
    timestamp: datetime
    vibration: float
    temperature: float
    rpm: float
    machine_status: str
    production_count: int
    good_count: int
    planned_production_time: float
    actual_run_time: float
    downtime_minutes: float

    class Config:
        from_attributes = True


class MaintenanceRecordOut(BaseModel):
    id: int
    machine_id: int
    maintenance_date: datetime
    maintenance_type: str
    failure_mode: Optional[str] = None
    description: Optional[str] = None
    part_used: Optional[str] = None

    class Config:
        from_attributes = True


class RiskHistoryPoint(BaseModel):
    timestamp: datetime
    risk_score: float


class AlertSummary(BaseModel):
    id: int
    risk_score: float
    severity: str
    maintenance_priority: float
    failure_mode: Optional[str] = None
    recommended_action: Optional[str] = None
    status: str
    created_at: datetime
    # Context required by the Machine Detail screen (spec §43).
    sensor_deviations: Optional[str] = None
    days_since_maintenance: Optional[float] = None
    previous_failure_context: Optional[str] = None
    machine_criticality: Optional[str] = None
    production_impact: Optional[str] = None
    part_needed: Optional[str] = None
    part_available: Optional[bool] = None
    explanation_text: Optional[str] = None
    degraded_mode: bool = False
    estimated_downtime_cost: Optional[float] = None
    estimated_loss_avoided: Optional[float] = None
    attribution: Optional[str] = None
    # Second, model-free signal plus how much the ML score can be relied on.
    deviation_score: Optional[float] = None
    deviation_detail: Optional[str] = None
    confidence: Optional[str] = None
    confidence_detail: Optional[str] = None

    class Config:
        from_attributes = True


class MachineDetailOut(BaseModel):
    id: int
    name: str
    type: str
    location: str
    install_date: str
    criticality: str
    nominal_vibration: float
    nominal_temperature: float
    nominal_rpm: float
    readings: List[SensorReadingOut] = []
    maintenance: List[MaintenanceRecordOut] = []
    risk_history: List[RiskHistoryPoint] = []
    latest_alert: Optional[AlertSummary] = None

    class Config:
        from_attributes = True


# ─── Alert Schemas ────────────────────────────────────────────────────────────

class AlertOut(BaseModel):
    id: int
    machine_id: int
    machine_name: Optional[str] = None
    machine_type: Optional[str] = None
    risk_score: float
    severity: str
    maintenance_priority: float
    top_sensors: Optional[str] = None
    sensor_deviations: Optional[str] = None
    failure_mode: Optional[str] = None
    days_since_maintenance: Optional[float] = None
    previous_failure_context: Optional[str] = None
    machine_criticality: Optional[str] = None
    production_impact: Optional[str] = None
    part_needed: Optional[str] = None
    part_available: Optional[bool] = None
    recommended_action: Optional[str] = None
    status: str
    created_at: datetime
    explanation_text: Optional[str] = None
    degraded_mode: bool = False
    # Modelled business impact (see cost_service.ASSUMPTIONS).
    estimated_downtime_cost: Optional[float] = None
    estimated_loss_avoided: Optional[float] = None
    attribution: Optional[str] = None
    # Second, model-free signal plus how much the ML score can be relied on.
    deviation_score: Optional[float] = None
    deviation_detail: Optional[str] = None
    confidence: Optional[str] = None
    confidence_detail: Optional[str] = None   # JSON list of model contributions

    class Config:
        from_attributes = True


class AlertUpdate(BaseModel):
    status: Optional[str] = None  # Acknowledged, Dismissed


class ExplainResponse(BaseModel):
    alert_id: int
    explanation: str
    source: str  # "gemini" or "deterministic"


# ─── Work Order Schemas ────────────────────────────────────────────────────────

class WorkOrderOut(BaseModel):
    id: int
    alert_id: Optional[int] = None
    machine_id: int
    machine_name: Optional[str] = None
    technician: Optional[str] = None
    part_needed: Optional[str] = None
    priority: str
    due_date: Optional[datetime] = None
    status: str
    created_at: datetime
    completed_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class WorkOrderUpdate(BaseModel):
    status: str  # Open, InProgress, Completed


# ─── OEE Schemas ─────────────────────────────────────────────────────────────

class OeeTrendPoint(BaseModel):
    timestamp: datetime
    oee: float
    availability: float
    performance: float
    quality: float


class MachineOeeSummary(BaseModel):
    machine_id: int
    machine_name: str
    oee: float
    availability: float
    performance: float
    quality: float


class OeeDashboardOut(BaseModel):
    plant_oee: float
    trend: List[OeeTrendPoint] = []
    per_machine: List[MachineOeeSummary] = []


# ─── OEE Loss Analysis ────────────────────────────────────────────────────────

class OeeLossItem(BaseModel):
    machine_id: int
    machine_name: str
    cost_center_code: Optional[str] = None
    oee: float
    availability: float
    performance: float
    quality: float
    availability_loss: float      # 0-1, share of ideal lost to each factor
    performance_loss: float
    quality_loss: float
    biggest_loss: str             # availability | performance | quality
    biggest_loss_pct: float
    estimated_gap_cost: float     # modelled cost of the gap to target


class OeeLossesOut(BaseModel):
    plant_oee: float
    target_oee: float
    window_hours: float
    currency: str = "USD"
    # Ranked worst-first, so the top of the list is where to act. This is the
    # Pareto view: a plant fixes the biggest loss, not the average.
    machines: List[OeeLossItem] = []
    loss_totals: dict = {}        # aggregate loss share by factor
    total_gap_cost: float = 0.0
    basis: str = ""


# ─── Dataset Schemas ──────────────────────────────────────────────────────────

class DatasetCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    description: Optional[str] = None
    source: str = "upload"


class DatasetOut(BaseModel):
    id: int
    name: str
    description: Optional[str] = None
    source: str
    status: str
    row_count: int
    machine_count: int
    column_mapping: Optional[str] = None
    notes: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True


class DatasetDeleteOut(BaseModel):
    dataset_id: int
    machines_deleted: int
    readings_deleted: int
    alerts_deleted: int
    oee_snapshots_deleted: int


class MachineCreate(BaseModel):
    """Register a machine. Nominals are optional: when omitted, the machine is
    marked as having no design spec and baselines are derived from its own data."""

    name: str = Field(..., min_length=1, max_length=120)
    type: str = "Unknown"
    location: str = "Unspecified"
    criticality: str = "Medium"
    install_date: Optional[str] = None
    nominal_vibration: Optional[float] = Field(None, ge=0)
    nominal_temperature: Optional[float] = Field(None, ge=-50, le=500)
    nominal_rpm: Optional[float] = Field(None, ge=0)
    cost_center_code: Optional[str] = None
    ideal_units_per_hour: Optional[float] = Field(None, gt=0)
    dataset_id: Optional[int] = None


# ─── Auth Schemas ─────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    # Accepts an email address as well as a seeded operator username, so the length
    # allows a full address rather than the old 64-character username bound.
    username: str = Field(..., min_length=1, max_length=254)
    password: str = Field(..., min_length=1, max_length=200)


class RegisterRequest(BaseModel):
    """Self-registration. Password rules live in `auth_service.validate_password`
    rather than in Field constraints, so the API can return a sentence explaining
    what is wrong instead of a schema error."""
    email: str = Field(..., min_length=3, max_length=254)
    password: str = Field(..., min_length=1, max_length=200)
    full_name: Optional[str] = Field(default=None, max_length=120)


class RegisterResponse(BaseModel):
    email: str
    role: str
    verification_required: bool
    # True only when a message was actually handed to an SMTP server.
    email_sent: bool = False
    # Present only when SMTP is not configured, so the flow remains completable in
    # development. Never populated once email is working, because a link in an API
    # response is readable by anyone who can see that response.
    verification_link: Optional[str] = None
    message: str


class VerifyResponse(BaseModel):
    verified: bool
    email: Optional[str] = None
    message: str


class VerifyRequest(BaseModel):
    token: str = Field(..., min_length=10, max_length=1024)


class LoginResponse(BaseModel):
    token: str
    username: str
    role: str
    full_name: Optional[str] = None
    permissions: List[str] = []
    email: Optional[str] = None


class CurrentUserOut(BaseModel):
    username: Optional[str] = None
    role: str = "viewer"
    permissions: List[str] = []
    auth_enabled: bool = True
    authenticated: bool = False


class AuditEntryOut(BaseModel):
    id: int
    actor: str
    role: Optional[str] = None
    action: str
    entity_type: str
    entity_id: Optional[int] = None
    details: Optional[str] = None
    timestamp: datetime

    class Config:
        from_attributes = True


class NotificationOut(BaseModel):
    id: int
    alert_id: Optional[int] = None
    channel: str
    target: Optional[str] = None
    status: str
    detail: Optional[str] = None
    timestamp: datetime

    class Config:
        from_attributes = True


# ─── Investigation Schemas ────────────────────────────────────────────────────

class InvestigateRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=500)
    machine_id: Optional[int] = None


class InvestigateResponse(BaseModel):
    answer: str
    source: str            # gemini | deterministic | cached | error
    intent: str
    intent_source: str     # gemini | keywords | none
    machine: Optional[str] = None
    # Returned so the UI can show what the answer was derived from. Grounding
    # that cannot be inspected is indistinguishable from a hallucination.
    evidence: List[dict] = []


class InvestigateSuggestions(BaseModel):
    intents: List[str] = []
    questions: List[str] = []


# ─── Business Impact Schemas ──────────────────────────────────────────────────

class MachineExposure(BaseModel):
    machine_id: int
    machine_name: str
    cost_center_code: Optional[str] = None
    risk_score: float
    severity: str
    downtime_cost_per_hour: float
    value_at_risk: float          # expected cost if it runs to failure
    loss_avoided_if_actioned: float


class ImpactOut(BaseModel):
    """Plant-level business impact. All figures are MODELLED estimates."""

    currency: str = "USD"
    value_at_risk: float                  # across all open alerts
    value_protected: float                # from completed work orders in window
    downtime_cost_incurred: float         # from recorded downtime in window
    window_days: int
    open_alert_count: int
    completed_work_order_count: int
    top_exposure: List[MachineExposure] = []
    assumptions: dict = {}
    basis: str = ""


# ─── Ingestion Schemas ────────────────────────────────────────────────────────

class SensorReadingIn(BaseModel):
    """One inbound OT reading.

    Every sensor is optional individually, but at least one must be present. Real
    plants rarely instrument every channel: a compressor may log oil temperature
    with no vibration transducer, a bearing rig may log vibration alone. Demanding
    all three would reject most genuine factory exports.

    A missing channel stays missing. It is never filled with a default, because a
    substituted value would be indistinguishable from a real measurement in every
    downstream calculation.
    """

    timestamp: Optional[datetime] = None
    vibration: Optional[float] = Field(None, ge=0)
    temperature: Optional[float] = Field(None, ge=-50, le=500)
    rpm: Optional[float] = Field(None, ge=0)
    machine_status: str = "Running"
    production_count: int = Field(0, ge=0)
    good_count: int = Field(0, ge=0)
    planned_production_time: float = Field(60.0, gt=0)
    actual_run_time: float = Field(60.0, ge=0)
    downtime_minutes: float = Field(0.0, ge=0)

    @model_validator(mode="after")
    def at_least_one_sensor(self):
        if self.vibration is None and self.temperature is None and self.rpm is None:
            raise ValueError(
                "at least one of vibration, temperature or rpm is required"
            )
        return self


class IngestRequest(BaseModel):
    """Identify the machine by id or by name, then supply one or more readings."""

    machine_id: Optional[int] = None
    machine_name: Optional[str] = None
    readings: List[SensorReadingIn] = Field(..., min_length=1, max_length=MAX_INGEST_ROWS)


class IngestRowError(BaseModel):
    row: int
    error: str


class IngestResponse(BaseModel):
    machine_id: int
    machine_name: str
    accepted: int
    rejected: int
    errors: List[IngestRowError] = []
    risk_score: Optional[float] = None
    severity: Optional[str] = None
    source: str


class IngestSourceStat(BaseModel):
    source: str
    readings: int
    latest_timestamp: Optional[datetime] = None


class IngestStatusOut(BaseModel):
    total_readings: int
    by_source: List[IngestSourceStat] = []


# ─── Dataset Upload Schemas ───────────────────────────────────────────────────
# Declared after the ingestion schemas because they reuse IngestRowError.

class ColumnMappingPreview(BaseModel):
    """What the detector made of the file, shown before anything is written."""

    headers: List[str] = []
    mapping: dict = {}                  # canonical field -> source column
    confidence: dict = {}               # canonical field -> exact | partial
    unmapped_headers: List[str] = []
    missing_required: List[str] = []
    present_sensors: List[str] = []
    missing_sensors: List[str] = []
    # False when a sensor channel is absent: the ML model needs all three, while the
    # deviation score and absolute limits work with whatever is present.
    ml_scoreable: bool = True
    detected_machines: List[str] = []
    sample_rows: List[dict] = []
    total_rows_previewed: int = 0
    timestamp_parse_failures: int = 0
    temperature_looks_fahrenheit: bool = False
    ready_to_commit: bool = False
    notes: List[str] = []


class UploadResult(BaseModel):
    dataset_id: int
    machines_created: int
    machines_matched: int
    readings_accepted: int
    readings_rejected: int
    errors: List[IngestRowError] = []
    machines: List[str] = []


# ─── ERP Schemas ──────────────────────────────────────────────────────────────

class CostCenterOut(BaseModel):
    id: int
    code: str
    name: str
    downtime_cost_per_hour: float
    currency: str
    erp_source: str

    class Config:
        from_attributes = True


class MaterialOut(BaseModel):
    id: int
    part_name: str
    erp_material_no: Optional[str] = None
    unit_cost: float
    lead_time_days: int
    supplier: Optional[str] = None
    erp_source: str
    # Joined from spare_parts so a planner sees cost and stock together.
    stock_quantity: Optional[int] = None
    is_available: Optional[bool] = None

    class Config:
        from_attributes = True


class ProductionOrderOut(BaseModel):
    id: int
    order_no: str
    machine_id: int
    machine_name: Optional[str] = None
    product: str
    planned_qty: int
    actual_qty: int
    scheduled_start: datetime
    scheduled_end: Optional[datetime] = None
    status: str
    erp_source: str

    class Config:
        from_attributes = True


class MachinePartContext(BaseModel):
    """The spare part a machine's current failure mode calls for.

    Cost, lead time and supplier are ERP master data; stock is maintenance-side.
    Together they answer "can we actually do this work, and when" — which neither
    system can answer alone.
    """

    part_name: str
    erp_material_no: Optional[str] = None
    unit_cost: Optional[float] = None
    lead_time_days: Optional[int] = None
    supplier: Optional[str] = None
    stock_quantity: Optional[int] = None
    is_available: bool = False


class MachineErpContextOut(BaseModel):
    """All IT-side context bearing on one machine's maintenance decision."""

    machine_id: int
    machine_name: str
    cost_center: Optional[CostCenterOut] = None
    part: Optional[MachinePartContext] = None
    # Resolved from the ERP schedule where orders exist; `basis` says which.
    production_impact: str
    production_impact_basis: str      # erp_production_orders | criticality_fallback
    production_impact_detail: str
    committed_units: int = 0
    open_orders: List[dict] = []
    assumptions: dict = {}


# ─── Health ───────────────────────────────────────────────────────────────────

class HealthOut(BaseModel):
    status: str
    demo_mode: bool
    database: str
    ml_model: str = "unknown"      # "loaded" or "unavailable"
    degraded_mode: bool = False    # True when scoring falls back to rules
    # First-boot seeding runs in the background, so a fresh deployment is briefly
    # reachable with an empty fleet. Reported so "still seeding" is distinguishable
    # from "seeding failed" without reading the logs.
    # not_started | running | completed | skipped | failed
    seed: str = "unknown"
    seed_detail: Optional[str] = None
    machines: int = 0
