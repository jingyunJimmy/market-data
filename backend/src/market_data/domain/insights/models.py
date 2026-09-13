"""The shapes intelligent insights are made of.

The flow these serve, one step per module in this package:

    QualityReport ─▶ EvidencePack ─▶ patterns ─▶ verify ─▶ suggestions ─▶ verify
                     evidence.py     provider    verify.py  provider      verify.py

Both a pattern and a suggestion are trusted as the provider states them, past
their schema: :mod:`verify` rejects anything malformed but does not check
either one against the :class:`Evidence` it cites.
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from market_data.domain.models import Frequency, IssueCategory, Severity

# --------------------------------------------------------------------------- #
# Evidence: what the code computed
# --------------------------------------------------------------------------- #


class HourShare(BaseModel):
    """One hour of the exchange-time clock and the occurrences that fall in it."""

    model_config = ConfigDict(frozen=True)

    hour: int
    """0-23, in ``InsightScope.timezone``."""
    count: int
    share: float
    """Of ``Evidence.analysed``, 0-1."""


class PeriodCount(BaseModel):
    """Occurrences in one calendar bucket, for reading a trend."""

    model_config = ConfigDict(frozen=True)

    start: dt.date
    count: int


class Evidence(BaseModel):
    """Distribution statistics for one quality finding.

    Every figure is optional beyond the counts because not every finding has
    every dimension: a duplicate has no duration, a daily finding has no hour.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    """``E1``, ``E2``, ... -- the handle patterns cite."""
    contract: str
    """The contract this finding was raised against."""
    code: str
    """The quality-check code, e.g. ``intra_session_gap`` -- what kind of finding this is."""
    category: IssueCategory
    """The check's broad grouping (gap, outlier, duplicate, ...), from the report."""
    severity: Severity
    """As the report assigned it -- unrelated to how much of a pattern the evidence shows."""
    occurrences: int
    """The finding's own count, as the report states it."""
    analysed: int
    """Occurrence rows the statistics below were computed from.

    Below ``occurrences`` when the per-issue cap cut the list, and zero for a
    rule that does not enumerate its occurrences at all.
    """
    per_1k_bars: float
    """``occurrences`` normalised by ``InsightScope.bars_checked``, so findings on
    contracts scanned over different amounts of data are comparable."""
    first_seen: dt.datetime | None = None
    """Instant of the earliest occurrence (UTC)."""
    last_seen: dt.datetime | None = None
    """Instant of the latest occurrence (UTC). Not the longest-running one --
    the one with the latest start."""
    expected_interval_s: int | None = None
    """The bar interval a gap was measured against. Gap findings only."""
    days_affected: int | None = None
    """Distinct exchange-time calendar dates any occurrence falls on."""
    days_affected_share: float | None = None
    """``days_affected`` over the trading days in scope, 0-1."""
    top_hours: list[HourShare] = Field(default_factory=list)
    """Up to three busiest exchange-time hours, busiest first. Minute data only."""
    weekday_counts: dict[str, int] = Field(default_factory=dict)
    """Occurrences per weekday (``Mon``..``Sun``), by exchange-time calendar date."""
    median_duration_min: float | None = None
    """Median of ``gap_minutes`` across occurrences. Gap findings only."""
    p90_duration_min: float | None = None
    """90th percentile of ``gap_minutes``, nearest-rank. Gap findings only."""
    magnitude_metric: str | None = None
    """Which figure ``max_magnitude`` is: ``mad_score`` or ``x_median``."""
    max_magnitude: float | None = None
    """The most extreme value of ``magnitude_metric`` seen. Outlier/spike findings only."""
    share_after_gap: float | None = None
    """Share of occurrences landing within ``InsightScope.after_gap_window_min``
    after a gap ends. Minute findings other than gaps only."""
    period: str | None = None
    """Bucket size of ``period_counts``: ``week``, ``month`` or ``quarter`` --
    chosen from how many calendar days the occurrences span, so a trend reads
    as a couple of dozen buckets rather than one number per week over years."""
    period_counts: list[PeriodCount] = Field(default_factory=list)
    """Occurrence counts per calendar bucket (see ``period``), in order, for reading a trend."""


class InsightScope(BaseModel):
    """What was examined, so a reader knows what the percentages are out of."""

    model_config = ConfigDict(frozen=True)

    contract: str | None
    frequency: Frequency
    start: dt.date | None
    end: dt.date | None
    bars_checked: int
    trading_days: int
    timezone: str
    after_gap_window_min: int


class EvidencePack(BaseModel):
    """The only input a provider sees. No bars, no raw occurrences."""

    model_config = ConfigDict(frozen=True)

    scope: InsightScope
    thresholds: dict[str, float]
    """The tunable quality settings in force, so a suggestion can reason about them."""
    evidence: list[Evidence]

    def by_id(self) -> dict[str, Evidence]:
        return {e.id: e for e in self.evidence}


# --------------------------------------------------------------------------- #
# Patterns and suggestions: what a provider concluded
# --------------------------------------------------------------------------- #


class PatternClassification(StrEnum):
    EXPECTED_MARKET_BEHAVIOR = "expected_market_behavior"
    """Normal for the market; the fix is to stop reporting it."""
    DATA_SOURCE_DEFECT = "data_source_defect"
    """The feed itself is wrong."""
    THRESHOLD_MISCALIBRATION = "threshold_miscalibration"
    """A check is tuned too tight or too loose for this data."""
    INGESTION_ARTIFACT = "ingestion_artifact"
    """Produced by how the data was loaded, not by the market or the vendor."""
    UNKNOWN = "unknown"


class Confidence(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Pattern(BaseModel):
    """A recurring regularity across one or more findings."""

    model_config = ConfigDict(frozen=True)

    id: str
    title: str
    classification: PatternClassification
    contracts: list[str]
    evidence_refs: list[str]
    explanation: str
    confidence: Confidence


class SuggestionType(StrEnum):
    """The closed set of rule changes a suggestion may propose.

    Closed on purpose: each maps onto something that exists in this system (a
    setting, an ingest predicate, a dedupe policy), so a suggestion is
    something an engineer can act on rather than advice. ``custom`` is the
    escape hatch, and the UI labels it as needing a human to design.
    See :mod:`market_data.domain.insights.catalogue`.
    """

    EXPECTED_WINDOW = "expected_window"
    ADJUST_THRESHOLD = "adjust_threshold"
    EXCLUDE_FROM_ANALYTICS = "exclude_from_analytics"
    REJECT_AT_INGEST = "reject_at_ingest"
    DEDUPE_POLICY = "dedupe_policy"
    CUSTOM = "custom"


class SuggestionKind(StrEnum):
    CLEANSING = "cleansing"
    """Changes what data is used."""
    VALIDATION = "validation"
    """Changes what is reported."""


ParamValue = str | int | float | bool | None


class Suggestion(BaseModel):
    model_config = ConfigDict(frozen=True)

    pattern_id: str
    type: SuggestionType
    kind: SuggestionKind
    params: dict[str, ParamValue] = Field(default_factory=dict)
    rationale: str


class Rejection(BaseModel):
    """Something a provider proposed that verification refused, and why.

    Returned rather than dropped silently: seeing what was thrown away is how a
    reader learns to trust what was kept.
    """

    model_config = ConfigDict(frozen=True)

    stage: str
    """``pattern`` or ``suggestion``."""
    item: str | None
    """The pattern id, or the title/type when there is no usable id."""
    reason: str


class InsightsReport(BaseModel):
    scope: InsightScope
    model: str | None = None
    """The model that read the evidence."""
    generated_at: dt.datetime
    evidence: list[Evidence]
    patterns: list[Pattern]
    suggestions: list[Suggestion]
    rejected: list[Rejection] = Field(default_factory=list)
