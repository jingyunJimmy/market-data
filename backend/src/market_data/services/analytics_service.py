"""Analytics orchestration: load filtered bars, run the pure analytics."""

from __future__ import annotations

import datetime as dt

from market_data.config import Settings, get_settings
from market_data.domain.analytics import daily_ohlcv, rolling_vwap
from market_data.domain.models import (
    ContractSummary,
    DailyBar,
    Frequency,
    VwapPoint,
    VwapSeries,
)
from market_data.storage.repository import BarRepository


class AnalyticsService:
    def __init__(self, repo: BarRepository, *, settings: Settings | None = None) -> None:
        self._repo = repo
        self._settings = settings or get_settings()

    def contracts(self) -> list[ContractSummary]:
        return self._repo.contracts()

    def daily_bars(
        self,
        *,
        contract: str | None = None,
        start: dt.date | None = None,
        end: dt.date | None = None,
        source: Frequency = Frequency.MINUTE,
    ) -> list[DailyBar]:
        """Daily OHLCV, aggregated from ``source`` frequency (minute by default)."""
        bars = self._repo.load_bars(contract=contract, frequency=source, start=start, end=end)
        return [DailyBar(**row) for row in daily_ohlcv(bars).to_dicts()]

    def vwap(
        self,
        *,
        contract: str,
        start: dt.date | None = None,
        end: dt.date | None = None,
        window_minutes: int | None = None,
        limit: int | None = None,
    ) -> VwapSeries:
        """Rolling VWAP over minute bars, keeping at most ``limit`` latest points.

        A wide date range on a liquid contract yields hundreds of thousands of
        points, so the series is capped and says so rather than silently
        thinning or overwhelming the caller.
        """
        window = window_minutes or self._settings.vwap_window_minutes
        bars = self._repo.load_bars(contract=contract, frequency=Frequency.MINUTE, start=start, end=end)
        frame = rolling_vwap(bars, window_minutes=window)
        kept = frame.tail(limit) if limit is not None and frame.height > limit else frame
        return VwapSeries(
            contract=contract,
            window_minutes=window,
            total_points=frame.height,
            truncated=kept.height < frame.height,
            points=[VwapPoint(**row) for row in kept.drop("contract").to_dicts()],
        )
