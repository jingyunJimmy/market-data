from __future__ import annotations

from fastapi import APIRouter, Depends

from market_data.api.deps import get_analytics_service
from market_data.domain.models import ContractSummary
from market_data.services import AnalyticsService

router = APIRouter(tags=["contracts"])


@router.get("/contracts", response_model=list[ContractSummary])
def list_contracts(svc: AnalyticsService = Depends(get_analytics_service)) -> list[ContractSummary]:
    """Everything ingested so far, with the date range available per frequency."""
    return svc.contracts()
