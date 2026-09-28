from sqlalchemy import Column, Integer, String, Float
from app.database import Base


class Material(Base):
    """ERP material master for a spare part.

    Deliberately joined to `spare_parts` by name rather than by foreign key, to
    match the existing loose-coupling convention (`work_orders.technician` and
    `work_orders.part_needed` are also name strings). `part_name` therefore must
    match `spare_parts.name` and the values in
    `recommendation_service.PART_MAP`.

    Supplies the two things the maintenance side cannot know on its own: what a
    part costs, and how long it takes to arrive.
    """

    __tablename__ = "materials"

    id = Column(Integer, primary_key=True, index=True)
    part_name = Column(String, unique=True, nullable=False)
    erp_material_no = Column(String, nullable=True)
    unit_cost = Column(Float, nullable=False, default=0.0)
    lead_time_days = Column(Integer, nullable=False, default=0)
    supplier = Column(String, nullable=True)
    erp_source = Column(String, nullable=False, default="ERP")
