"""
Investigation API — POST /api/investigate, GET /api/investigate/suggestions

Natural-language root-cause investigation. Answers are grounded in fixed database
retrieval; the LLM classifies intent and phrases prose but never writes SQL and
never supplies facts. The `evidence` array in the response is what the answer was
built from, so any claim can be checked.
"""
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.llm.investigate_service import (
    INTENTS,
    SUGGESTED_QUESTIONS,
    investigate,
)
from app.schemas import (
    InvestigateRequest,
    InvestigateResponse,
    InvestigateSuggestions,
)
from app.security import limit_llm_requests

router = APIRouter(prefix="/api/investigate", tags=["investigate"])


@router.post(
    "",
    response_model=InvestigateResponse,
    dependencies=[Depends(limit_llm_requests)],
)
async def ask(payload: InvestigateRequest, db: AsyncSession = Depends(get_db)):
    """Answer a question about the plant, grounded in its data.

    Rate-limited per client: each call can reach a metered third-party model, so an
    open endpoint is someone else's bill and a route to exhausting the daily quota
    that every other user depends on.
    """
    result = await investigate(db, payload.question, payload.machine_id)
    return InvestigateResponse(**result)


@router.get("/suggestions", response_model=InvestigateSuggestions)
async def suggestions():
    """Starter questions and the intents the service can answer."""
    return InvestigateSuggestions(
        intents=list(INTENTS),
        questions=SUGGESTED_QUESTIONS,
    )
