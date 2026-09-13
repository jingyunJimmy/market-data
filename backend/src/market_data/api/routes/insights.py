from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, status

from market_data.api.deps import get_insights_service
from market_data.domain.insights import InsightsReport
from market_data.domain.models import Frequency
from market_data.llm import InsightProviderError
from market_data.services import InsightsService

router = APIRouter(prefix="/insights", tags=["insights"])


@router.post(
    "",
    response_model=InsightsReport,
    responses={503: {"description": "The LLM produced no usable answer; `detail` says why."}},
)
def generate_insights(
    contract: str,
    frequency: Frequency = Frequency.MINUTE,
    start: dt.date | None = None,
    end: dt.date | None = None,
    svc: InsightsService = Depends(get_insights_service),
) -> InsightsReport:
    """Identify recurring quality patterns and suggest rules for one contract and range.

    POST rather than GET: every call does real work and asks an LLM, which can
    take tens of seconds and cost money. The dashboard only issues it when the
    user asks, never as a side effect of moving a filter.
    """
    try:
        return svc.generate(contract=contract, frequency=frequency, start=start, end=end)
    except InsightProviderError as exc:
        # Not a 500: the application is fine, the model behind it is not. The
        # message names the endpoint and never a key, so it is safe to show.
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
