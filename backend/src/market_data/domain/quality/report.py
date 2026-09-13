"""Run every registered rule over a bar frame and assemble a QualityReport."""

from __future__ import annotations

import datetime as dt

import polars as pl

from market_data.config import Settings, get_settings
from market_data.domain.models import Frequency, QualityIssue, QualityReport
from market_data.domain.quality.rules import RuleContext, RuleFn, all_rules


class QualityValidator:
    def __init__(self, *, settings: Settings | None = None, rules: tuple[RuleFn, ...] | None = None) -> None:
        self._settings = settings or get_settings()
        self._rules = rules if rules is not None else all_rules()

    def validate(
        self,
        bars: pl.DataFrame,
        *,
        contract: str | None = None,
        frequency: Frequency = Frequency.MINUTE,
        start: dt.date | None = None,
        end: dt.date | None = None,
        superseded: pl.DataFrame | None = None,
    ) -> QualityReport:
        ctx = RuleContext(
            bars=bars,
            frequency=frequency,
            settings=self._settings,
            superseded=superseded if superseded is not None else pl.DataFrame(),
        )
        issues: list[QualityIssue] = []
        for fn in self._rules:
            issues.extend(fn(ctx))
        issues.sort(key=lambda i: (-i.severity.rank, i.category, i.contract, i.code))
        return QualityReport(
            contract=contract,
            frequency=frequency,
            start=start,
            end=end,
            bars_checked=bars.height,
            issues=issues,
        )
