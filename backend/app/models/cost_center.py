from sqlalchemy import Column, Integer, String, Float
from app.database import Base


class CostCenter(Base):
    """Financial owner of a machine, sourced from the ERP side (spec: IT/OT convergence).

    `downtime_cost_per_hour` is what makes unplanned downtime expressible in money
    rather than minutes. Every currency figure derived from it is a MODELLED
    estimate, never a measured saving.
    """

    __tablename__ = "cost_centers"

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String, unique=True, nullable=False)
    name = Column(String, nullable=False)
    downtime_cost_per_hour = Column(Float, nullable=False, default=0.0)
    currency = Column(String, nullable=False, default="USD")
    # Marks the record as ERP-originated so a real sync can be distinguished
    # from seeded demo data.
    erp_source = Column(String, nullable=False, default="ERP")
