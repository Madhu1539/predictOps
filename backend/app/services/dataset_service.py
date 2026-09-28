"""
Dataset helpers — machine naming and provisioning.

`machines.name` is globally unique, so an uploaded machine called `M-102` would
collide with the demo fleet's `M-102`. Rather than drop that constraint (which
protects the demo fleet from duplicate seeding), names are namespaced per dataset
and the user's original label is preserved in `display_name` for the UI.
"""
import logging
from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.machine import ORIGIN_EXTERNAL, Machine

logger = logging.getLogger(__name__)


def scoped_name(name: str, dataset_id: Optional[int]) -> str:
    """Storage name for a machine.

    The demo fleet (dataset_id None) keeps its bare names so nothing existing
    changes. Dataset machines are prefixed, which is invisible to the user
    because `display_name` is what gets rendered.
    """
    clean = (name or "").strip()
    if dataset_id is None:
        return clean
    return f"ds{dataset_id}:{clean}"


def display_of(machine: Machine) -> str:
    """What to show the user: the original label when we have it."""
    return machine.display_name or machine.name


async def get_or_create_machine(
    db: AsyncSession,
    name: str,
    dataset_id: Optional[int],
    machine_type: str = "Unknown",
    location: str = "Unspecified",
    criticality: str = "Medium",
) -> tuple[Machine, bool]:
    """Fetch a dataset's machine by user-facing name, creating it if absent.

    Returns `(machine, created)`. Machines created this way are marked
    `external` and carry no design spec, so the simulator will not touch them and
    baselines will be derived from their own readings rather than assumed.
    """
    stored = scoped_name(name, dataset_id)
    existing = (
        await db.execute(select(Machine).where(Machine.name == stored))
    ).scalar_one_or_none()
    if existing is not None:
        return existing, False

    machine = Machine(
        name=stored,
        display_name=name.strip(),
        type=machine_type,
        location=location,
        criticality=criticality,
        install_date=datetime.utcnow().strftime("%Y-%m-%d"),
        dataset_id=dataset_id,
        data_origin=ORIGIN_EXTERNAL,
        # Nothing was supplied, so nominals are placeholders until a baseline is
        # derived. Recorded honestly rather than presented as a specification.
        has_design_spec=0,
    )
    db.add(machine)
    await db.flush()
    return machine, True
