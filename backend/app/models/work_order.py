from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Float
from app.database import Base
from datetime import datetime


class WorkOrder(Base):
    __tablename__ = "work_orders"

    id = Column(Integer, primary_key=True, index=True)
    alert_id = Column(Integer, ForeignKey("alerts.id"), nullable=True)
    machine_id = Column(Integer, ForeignKey("machines.id"), nullable=False)
    technician = Column(String, nullable=True)
    part_needed = Column(String, nullable=True)
    priority = Column(String, nullable=False, default="Medium")  # Urgent, Medium, Low
    due_date = Column(DateTime, nullable=True)
    status = Column(String, nullable=False, default="Open")  # Open, InProgress, Completed
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)
