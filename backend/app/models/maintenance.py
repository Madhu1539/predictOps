from sqlalchemy import Column, Integer, String, DateTime, ForeignKey
from app.database import Base
from datetime import datetime


class MaintenanceRecord(Base):
    __tablename__ = "maintenance_records"

    id = Column(Integer, primary_key=True, index=True)
    machine_id = Column(Integer, ForeignKey("machines.id"), nullable=False)
    maintenance_date = Column(DateTime, nullable=False)
    maintenance_type = Column(String, nullable=False)  # Preventive, Corrective, Emergency
    failure_mode = Column(String, nullable=True)       # BEARING_DEGRADATION, OVERHEATING, etc.
    description = Column(String, nullable=True)
    part_used = Column(String, nullable=True)
