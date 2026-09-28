from sqlalchemy import Column, Integer, String, DateTime
from app.database import Base
from datetime import datetime


class Dataset(Base):
    """A named collection of machines and readings from one source.

    The built-in demo fleet is deliberately NOT a row here: its machines carry
    `dataset_id IS NULL`. That keeps every pre-existing query working untouched
    and means "the demo fleet" needs no migration.

    A dataset exists so an uploaded factory's data can be reported on and then
    removed cleanly, without ever touching the demo fleet.
    """

    __tablename__ = "datasets"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    description = Column(String, nullable=True)
    # How the data arrived: "upload" (CSV) or "api" (batch POST).
    source = Column(String, nullable=False, default="upload")
    # draft  -> created, nothing committed yet
    # ready  -> readings committed and scored
    # failed -> validation rejected the input
    status = Column(String, nullable=False, default="draft")
    row_count = Column(Integer, nullable=False, default=0)
    machine_count = Column(Integer, nullable=False, default=0)
    # Column mapping actually used, stored as JSON so a report can show how the
    # user's headers were interpreted.
    column_mapping = Column(String, nullable=True)
    notes = Column(String, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
