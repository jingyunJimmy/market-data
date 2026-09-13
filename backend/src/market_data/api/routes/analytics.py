from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, Query

from market_data.api.deps import get_analytics_service
from market_data.domain.models import DailyBar, Frequency, VwapSeries
from market_data.services import AnalyticsService

router = APIRouter(prefix="/analytics", tags=["analytics"])


@router.get("/daily-ohlcv", response_model=list[DailyBar])
def daily_ohlcv(
    contract: str,
    start: dt.date | None = None,
    end: dt.date | None = None,
    source: Frequency = Query(
        default=Frequency.MINUTE,
        description="Aggregate minute bars into sessions, or pass vendor daily bars through.",
    ),
    svc: AnalyticsService = Depends(get_analytics_service),
) -> list[DailyBar]:
    return svc.daily_bars(contract=contract, start=start, end=end, source=source)


@router.get("/vwap", response_model=VwapSeries)
def vwap(
    contract: str,
    start: dt.date | None = None,
    end: dt.date | None = None,
    window_minutes: int = Query(default=15, ge=1, le=1440),
    limit: int = Query(default=50_000, ge=1, le=200_000),
    svc: AnalyticsService = Depends(get_analytics_service),
) -> VwapSeries:
    return svc.vwap(contract=contract, start=start, end=end, window_minutes=window_minutes, limit=limit)
