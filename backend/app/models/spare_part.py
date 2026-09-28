from sqlalchemy import Column, Integer, String, Float
from app.database import Base


class SparePart(Base):
    __tablename__ = "spare_parts"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, nullable=False)
    stock_quantity = Column(Integer, nullable=False, default=0)
    minimum_stock = Column(Integer, nullable=False, default=1)

    @property
    def is_available(self) -> bool:
        return self.stock_quantity >= self.minimum_stock
