from sqlalchemy import Column, Integer, String, Float, Date, Enum, ForeignKey, Index
from app.database import Base
import enum


class CriticalityLevel(str, enum.Enum):
    LOW = "Low"
    MEDIUM = "Medium"
    HIGH = "High"


class MachineType(str, enum.Enum):
    CNC = "CNC Machine"
    HYDRAULIC_PRESS = "Hydraulic Press"
    CONVEYOR_MOTOR = "Conveyor Motor"
    INDUSTRIAL_PUMP = "Industrial Pump"


# The only values that legitimately change pipeline behaviour. `simulated` means
# the live feed owns this machine and may append generated readings to it;
# `external` means the data came from outside and must never be extended with
# fabricated values.
ORIGIN_SIMULATED = "simulated"
ORIGIN_EXTERNAL = "external"


class Machine(Base):
    __tablename__ = "machines"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, nullable=False)
    # Name as the user supplied it. `name` may be namespaced per dataset to avoid
    # collisions with the demo fleet; this is what gets displayed.
    display_name = Column(String, nullable=True)
    type = Column(String, nullable=False)
    location = Column(String, nullable=False)
    install_date = Column(String, nullable=False)  # stored as ISO date string
    criticality = Column(String, nullable=False, default="Medium")
    # Design specification. Authoritative when known; see baseline_service for the
    # precedence rule when it is not.
    nominal_vibration = Column(Float, nullable=False, default=2.0)
    nominal_temperature = Column(Float, nullable=False, default=65.0)
    nominal_rpm = Column(Float, nullable=False, default=1450.0)
    # ERP linkage. Kept as a code rather than a foreign key to match the existing
    # loose-coupling convention elsewhere in the schema.
    cost_center_code = Column(String, nullable=True)
    # Design throughput in units/hour. Nullable so machines seeded before this
    # existed fall back to the plant default in oee_service.
    ideal_units_per_hour = Column(Float, nullable=True)

    # ── Provenance ────────────────────────────────────────────────────────────
    # Defaults to `simulated` so every pre-existing row keeps its current
    # behaviour without a data migration.
    data_origin = Column(String, nullable=False, default=ORIGIN_SIMULATED)
    # NULL = the built-in demo fleet.
    dataset_id = Column(Integer, ForeignKey("datasets.id"), nullable=True, index=True)

    # ── Observed baselines, derived from this machine's own history ───────────
    # Distinct from nominal_*: nominal is the design spec, these are what this
    # machine actually looks like when running normally. Nullable until derived.
    baseline_vibration = Column(Float, nullable=True)
    baseline_temperature = Column(Float, nullable=True)
    baseline_rpm = Column(Float, nullable=True)
    baseline_method = Column(String, nullable=True)
    baseline_sample_count = Column(Integer, nullable=True)
    # True when the design spec was supplied rather than assumed, which decides
    # whether nominal_* or baseline_* is the reference for deviations.
    has_design_spec = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        Index("ix_machines_dataset_origin", "dataset_id", "data_origin"),
    )

    @property
    def label(self) -> str:
        """User-facing name.

        `name` may be namespaced per dataset to avoid colliding with the demo
        fleet, so anything shown to a person must use this instead.
        """
        return self.display_name or self.name
