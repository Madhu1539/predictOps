# Models package - import all to ensure they are registered with SQLAlchemy
from app.models.machine import Machine
from app.models.sensor import SensorReading
from app.models.maintenance import MaintenanceRecord
from app.models.alert import Alert
from app.models.work_order import WorkOrder
from app.models.oee import OeeSnapshot
from app.models.spare_part import SparePart
from app.models.technician import Technician
from app.models.cost_center import CostCenter
from app.models.material import Material
from app.models.production_order import ProductionOrder
from app.models.auth import User, AuditLog, NotificationLog
from app.models.dataset import Dataset

__all__ = [
    "Machine",
    "SensorReading",
    "MaintenanceRecord",
    "Alert",
    "WorkOrder",
    "OeeSnapshot",
    "SparePart",
    "Technician",
    "CostCenter",
    "Material",
    "ProductionOrder",
    "User",
    "AuditLog",
    "NotificationLog",
    "Dataset",
]
