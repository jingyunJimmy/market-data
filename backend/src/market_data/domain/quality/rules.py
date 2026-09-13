"""Rule protocol + the list of built-in checks.

A rule is a pure function ``(RuleContext) -> Iterable[QualityIssue]``.
:func:`all_rules` names every one of them explicitly, and
:class:`~market_data.domain.quality.report.QualityValidator` runs them in that
order. Adding a check is writing the function in ``checks/`` and adding its
name to that list.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

import polars as pl

from market_data.config import Settings, get_settings
from market_data.domain.models import Frequency, QualityIssue


@dataclass(frozen=True)
class RuleContext:
    """Everything a rule needs. Frames are already filtered by the caller."""

    bars: pl.DataFrame
    frequency: Frequency
    settings: Settings = field(default_factory=get_settings)
    #: Valid bars ingestion displaced under last-write-wins. Empty for rules
    #: run over a raw frame that was never persisted.
    superseded: pl.DataFrame = field(default_factory=lambda: pl.DataFrame())

    def per_contract(self) -> Iterable[tuple[str, pl.DataFrame]]:
        if self.bars.is_empty():
            return []
        return (
            (str(c), grp.sort("ts")) for (c, *_), grp in self.bars.group_by("contract", maintain_order=True)
        )


RuleFn = Callable[[RuleContext], Iterable[QualityIssue]]


def all_rules() -> tuple[RuleFn, ...]:
    """Every built-in check, in the order the validator runs them.

    Imported inside the function because the check modules import
    :class:`RuleContext` from here; at module level the two would form a cycle.
    """
    from market_data.domain.quality.checks.duplicates import (
        conflicting_duplicates,
        exact_duplicates,
        resolved_instant_conflicts,
    )
    from market_data.domain.quality.checks.gaps import timestamp_gaps
    from market_data.domain.quality.checks.outliers import (
        price_return_outliers,
        volume_spikes,
    )
    from market_data.domain.quality.checks.values import invalid_values

    return (
        exact_duplicates,
        conflicting_duplicates,
        resolved_instant_conflicts,
        timestamp_gaps,
        price_return_outliers,
        volume_spikes,
        invalid_values,
    )
