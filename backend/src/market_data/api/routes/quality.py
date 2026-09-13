from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, Query

from market_data.api.deps import get_quality_service
from market_data.domain.models import (
    Frequency,
    IssueDetailPage,
    MissingTimestampPage,
    QualityReport,
)
from market_data.services import QualityService

router = APIRouter(prefix="/quality", tags=["quality"])


@router.get("/report", response_model=QualityReport)
def quality_report(
    contract: str,
    frequency: Frequency = Frequency.MINUTE,
    start: dt.date | None = None,
    end: dt.date | None = None,
    svc: QualityService = Depends(get_quality_service),
) -> QualityReport:
    return svc.report(contract=contract, frequency=frequency, start=start, end=end)


@router.get("/missing-timestamps", response_model=MissingTimestampPage)
def missing_timestamps(
    contract: str,
    frequency: Frequency = Frequency.MINUTE,
    start: dt.date | None = None,
    end: dt.date | None = None,
    limit: int = Query(default=2000, ge=1, le=20_000),
    offset: int = Query(default=0, ge=0),
    svc: QualityService = Depends(get_quality_service),
) -> MissingTimestampPage:
    """Every instant with no bar, flat and chronological.

    Separate from ``/report`` because of volume: one thin contract can be short
    six figures of bars. Paged so the client can reach the end of the list
    rather than being handed an unmarked truncation.
    """
    return svc.missing_timestamps(
        contract=contract, frequency=frequency, start=start, end=end, limit=limit, offset=offset
    )


@router.get("/issue-details", response_model=IssueDetailPage)
def issue_details(
    contract: str,
    code: str,
    frequency: Frequency = Frequency.MINUTE,
    start: dt.date | None = None,
    end: dt.date | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    svc: QualityService = Depends(get_quality_service),
) -> IssueDetailPage:
    """Page through one finding's evidence.

    ``/report`` carries a capped sample of every issue's details so the table
    renders at once; this reaches the rest of a list the report only sampled.
    """
    return svc.issue_details(
        code=code,
        contract=contract,
        frequency=frequency,
        start=start,
        end=end,
        limit=limit,
        offset=offset,
    )
