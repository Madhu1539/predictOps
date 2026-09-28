from sqlalchemy import Column, Integer, String
from app.database import Base


class Technician(Base):
    __tablename__ = "technicians"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, nullable=False)
    active_work_orders = Column(Integer, nullable=False, default=0)
