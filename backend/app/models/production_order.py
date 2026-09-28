from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Index
from app.database import Base
from datetime import datetime


class ProductionOrder(Base):
    """ERP production order scheduled against a machine.

    This is the IT-side context that makes a maintenance decision a business
    decision: taking a machine down matters more when a large order is scheduled
    on it. Planned vs actual quantity also gives OEE a scheduling reference that
    sensor data alone cannot provide.
    """

    __tablename__ = "production_orders"

    id = Column(Integer, primary_key=True, index=True)
    order_no = Column(String, unique=True, nullable=False)
    machine_id = Column(Integer, ForeignKey("machines.id"), nullable=False)
    product = Column(String, nullable=False)
    planned_qty = Column(Integer, nullable=False, default=0)
    actual_qty = Column(Integer, nullable=False, default=0)
    scheduled_start = Column(DateTime, nullable=False, default=datetime.utcnow)
    scheduled_end = Column(DateTime, nullable=True)
    status = Column(String, nullable=False, default="Released")  # Released, InProgress, Complete
    erp_source = Column(String, nullable=False, default="ERP")

    __table_args__ = (
        Index("ix_production_orders_machine_start", "machine_id", "scheduled_start"),
    )
