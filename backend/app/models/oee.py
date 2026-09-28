from sqlalchemy import Column, Integer, Float, DateTime, ForeignKey, Index
from app.database import Base
from datetime import datetime


class OeeSnapshot(Base):
    __tablename__ = "oee_snapshots"

    id = Column(Integer, primary_key=True, index=True)
    machine_id = Column(Integer, ForeignKey("machines.id"), nullable=False)
    timestamp = Column(DateTime, nullable=False, default=datetime.utcnow)
    availability = Column(Float, nullable=False)   # 0-1
    performance = Column(Float, nullable=False)    # 0-1
    quality = Column(Float, nullable=False)        # 0-1
    oee = Column(Float, nullable=False)            # 0-1

    # Index for time series queries
    __table_args__ = (
        Index("ix_oee_snapshots_machine_timestamp", "machine_id", "timestamp"),
    )
