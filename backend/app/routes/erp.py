"""
ERP API — GET /api/erp/cost-centers, GET /api/erp/materials,
GET /api/erp/production-orders

Exposes the IT-side master data that PredictOps converges with OT sensor data.
These are read-only views: PredictOps is a consumer of ERP data, not its owner.
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from typing import List, Optional

from app.database import get_db
from app.models.alert import Alert
from app.models.cost_center import CostCenter
from app.models.material import Material
from app.models.production_order import ProductionOrder
from app.models.spare_part import SparePart
from app.models.machine import Machine
from app.schemas import (
    CostCenterOut,
    MachineErpContextOut,
    MachinePartContext,
    MaterialOut,
    ProductionOrderOut,
)
from app.services.priority_service import (
    ASSUMPTIONS as PRIORITY_ASSUMPTIONS,
    resolve_production_impact,
)

router = APIRouter(prefix="/api/erp", tags=["erp"])

# An alert in one of these states still describes the machine's current condition.
OPEN_ALERT_STATUSES = ("Active", "Acknowledged")



@router.get("/cost-centers", response_model=List[CostCenterOut])
async def list_cost_centers(db: AsyncSession = Depends(get_db)):
    """Cost centres, including the downtime cost per hour used by the value model."""
    result = await db.execute(select(CostCenter).order_by(CostCenter.code))
    return [CostCenterOut.model_validate(cc) for cc in result.scalars().all()]


@router.get("/materials", response_model=List[MaterialOut])
async def list_materials(db: AsyncSession = Depends(get_db)):
    """Material master joined to on-hand stock.

    Cost and lead time come from ERP; stock comes from the maintenance side.
    Showing them together is the point of the convergence.
    """
    query = (
        select(Material, SparePart)
        .join(SparePart, SparePart.name == Material.part_name, isouter=True)
        .order_by(Material.part_name)
    )
    result = await db.execute(query)

    output = []
    for material, part in result.all():
        item = MaterialOut.model_validate(material)
        if part is not None:
            item.stock_quantity = part.stock_quantity
            item.is_available = part.is_available
        output.append(item)
    return output


@router.get("/production-orders", response_model=List[ProductionOrderOut])
async def list_production_orders(
    machine_id: Optional[int] = Query(None),
    status: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Production orders scheduled against machines.

    Filters are applied in SQL before LIMIT so a filtered query cannot come back
    empty while matching rows exist beyond the first 100.
    """
    query = (
        select(ProductionOrder, Machine.name)
        .join(Machine, Machine.id == ProductionOrder.machine_id, isouter=True)
    )
    if machine_id is not None:
        query = query.where(ProductionOrder.machine_id == machine_id)
    if status:
        query = query.where(ProductionOrder.status == status)
    query = query.order_by(ProductionOrder.scheduled_start.desc()).limit(100)

    result = await db.execute(query)

    output = []
    for order, machine_name in result.all():
        item = ProductionOrderOut.model_validate(order)
        item.machine_name = machine_name
        output.append(item)
    return output


@router.get("/machine-context/{machine_id}", response_model=MachineErpContextOut)
async def machine_erp_context(machine_id: int, db: AsyncSession = Depends(get_db)):
    """Every piece of IT-side context bearing on one machine's maintenance decision.

    Assembled in one call because this is the convergence made concrete: the cost
    centre that prices an hour of downtime, the spare part with its stock and lead
    time, and the production orders a failure would actually disrupt. Split across
    three round trips the UI would show them as unrelated tables.
    """
    machine = (
        await db.execute(select(Machine).where(Machine.id == machine_id))
    ).scalar_one_or_none()
    if machine is None:
        raise HTTPException(status_code=404, detail="MACHINE_NOT_FOUND")

    cost_center = None
    if machine.cost_center_code:
        cost_center = (
            await db.execute(
                select(CostCenter).where(CostCenter.code == machine.cost_center_code)
            )
        ).scalar_one_or_none()

    # The part this machine's current failure mode calls for, if it has an open
    # alert. Without one there is nothing to procure yet.
    alert = (
        await db.execute(
            select(Alert)
            .where(Alert.machine_id == machine_id)
            .where(Alert.status.in_(OPEN_ALERT_STATUSES))
            .order_by(Alert.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    part_context = None
    if alert is not None and alert.part_needed:
        material = (
            await db.execute(
                select(Material).where(Material.part_name == alert.part_needed)
            )
        ).scalar_one_or_none()
        spare = (
            await db.execute(
                select(SparePart).where(SparePart.name == alert.part_needed)
            )
        ).scalar_one_or_none()
        part_context = MachinePartContext(
            part_name=alert.part_needed,
            erp_material_no=material.erp_material_no if material else None,
            unit_cost=material.unit_cost if material else None,
            lead_time_days=material.lead_time_days if material else None,
            supplier=material.supplier if material else None,
            stock_quantity=spare.stock_quantity if spare else None,
            # Fail closed, matching the alerting path: an unknown part is not
            # assumed to be on the shelf.
            is_available=spare.is_available if spare else False,
        )

    # Production impact recomputed here rather than read off the alert, so a
    # machine with no open alert still shows what is committed to run on it.
    impact = await resolve_production_impact(db, machine_id, machine.criticality)

    return MachineErpContextOut(
        machine_id=machine_id,
        machine_name=machine.label,
        cost_center=CostCenterOut.model_validate(cost_center) if cost_center else None,
        part=part_context,
        production_impact=impact["impact"],
        production_impact_basis=impact["basis"],
        production_impact_detail=impact["detail"],
        committed_units=impact["committed_units"],
        open_orders=impact["orders"],
        assumptions=PRIORITY_ASSUMPTIONS,
    )
