"""Core domain models shared across every layer.

The canonical bar schema is the contract between ingestion and everything
downstream. Raw vendor files (whatever their column names or timestamp
conventions) are normalised into :data:`CANONICAL_COLUMNS` before storage.
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum

import polars as pl
from pydantic import BaseModel, ConfigDict, Field


class Frequency(StrEnum):
    MINUTE = "minute"
    DAILY = "daily"


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"

    @property
    def rank(self) -> int:
        return {"info": 0, "warning": 1, "error": 2}[self.value]


class IssueCategory(StrEnum):
    GAP = "gap"
    DUPLICATE = "duplicate"
    CONFLICT = "conflict"
    INVALID_VALUE = "invalid_value"
    OUTLIER = "outlier"
    TIMESTAMP = "timestamp"


# --------------------------------------------------------------------------- #
# Canonical bar schema
# --------------------------------------------------------------------------- #

#: Column order + dtypes every stored bar frame must conform to.
CANONICAL_SCHEMA: pl.Schema = pl.Schema(
    {  # type: ignore[arg-type]  # mixed DataType instances + classes -> mypy infers object
        "contract": pl.String,
        "root": pl.String,
        "exchange": pl.String,
        "frequency": pl.String,
        "ts": pl.Datetime(time_unit="us", time_zone="UTC"),
        "trading_date": pl.Date,
        "open": pl.Float64,
        "high": pl.Float64,
        "low": pl.Float64,
        "close": pl.Float64,
        "volume": pl.Int64,
        "open_interest": pl.Int64,
        "source_file": pl.String,
        "row_hash": pl.String,
        "ingested_at": pl.Datetime(time_unit="us", time_zone="UTC"),
    }
)

CANONICAL_COLUMNS: tuple[str, ...] = tuple(CANONICAL_SCHEMA)
OHLC_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close")


# --------------------------------------------------------------------------- #
# Ingestion
# --------------------------------------------------------------------------- #


class IngestionResult(BaseModel):
    run_id: str
    source: str
    started_at: dt.datetime
    finished_at: dt.datetime
    rows_read: int
    rows_ingested: int
    rows_rejected: int
    rows_deduplicated: int = 0
    rows_superseded: int = 0
    """Valid rows displaced by a later row for the same instant (last write wins)."""
    rejections_by_reason: dict[str, int] = Field(default_factory=dict)
    contracts: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Catalogue
# --------------------------------------------------------------------------- #


class ContractSummary(BaseModel):
    """What has been ingested for one ``(contract, frequency)`` pair.

    The date bounds let a client open on a range that actually contains data
    instead of guessing.
    """

    model_config = ConfigDict(frozen=True)

    contract: str
    root: str
    exchange: str
    frequency: Frequency
    first_date: dt.date
    last_date: dt.date
    bars: int


# --------------------------------------------------------------------------- #
# Analytics
# --------------------------------------------------------------------------- #


class DailyBar(BaseModel):
    """One session's OHLCV, aggregated from intraday bars or passed through."""

    model_config = ConfigDict(frozen=True)

    contract: str
    trading_date: dt.date
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    volume: int | None
    open_interest: int | None
    bar_count: int


class VwapPoint(BaseModel):
    """Trailing-window VWAP at one bar timestamp."""

    model_config = ConfigDict(frozen=True)

    ts: dt.datetime
    typical_price: float | None
    vwap: float | None


class VwapSeries(BaseModel):
    """A VWAP series plus the metadata a chart needs to label itself honestly."""

    contract: str
    window_minutes: int
    total_points: int
    truncated: bool
    """True when ``points`` holds only the most recent ``len(points)`` of ``total_points``."""
    points: list[VwapPoint]


# --------------------------------------------------------------------------- #
# Data quality
# --------------------------------------------------------------------------- #


class IssueDetail(BaseModel):
    """One concrete occurrence behind a :class:`QualityIssue`.

    An issue is a *summary* -- "9594 intra-session gaps" -- which tells a user
    that something is wrong but not where to look. A detail is one row of the
    evidence: which instant, and whatever the rule wants to say about it.

    The shape is deliberately category-agnostic, so gap, duplicate, outlier and
    invalid-value rules can all populate it without the API growing a variant
    per check:

    * ``ts``      -- the instant the occurrence starts at.
    * ``end_ts``  -- set only when the occurrence spans a range (a run of
      missing bars); ``None`` when it is a single instant.
    * ``label``   -- an optional short string for rules whose natural identity
      is not a timestamp (a daily session is a *date*, not a UTC midnight).
      The UI falls back to formatting ``ts`` when this is absent.
    * ``values``  -- free-form per-occurrence figures. The dashboard renders one
      column per key it finds, so a new rule needs no UI change to show its own
      numbers (missing bar counts, the offending price, a z-score, ...).
    """

    model_config = ConfigDict(frozen=True)

    ts: dt.datetime | None = None
    end_ts: dt.datetime | None = None
    label: str | None = None
    values: dict[str, float | int | str | None] = Field(default_factory=dict)


class QualityIssue(BaseModel):
    model_config = ConfigDict(frozen=True)

    category: IssueCategory
    code: str
    severity: Severity
    contract: str
    frequency: Frequency
    message: str
    count: int = 1
    start_ts: dt.datetime | None = None
    end_ts: dt.datetime | None = None
    context: dict[str, object] = Field(default_factory=dict)

    # -- evidence ------------------------------------------------------- #
    # Populated through `domain.quality.details.detail_fields`, which keeps the
    # three fields consistent. A rule that has not been taught to enumerate its
    # occurrences yet simply leaves them at their defaults, and the dashboard
    # renders the summary row on its own as before.
    details: list[IssueDetail] = Field(default_factory=list)
    """A capped, chronological sample of the occurrences behind this issue."""
    detail_total: int = 0
    """How many occurrences exist, which is >= ``len(details)``."""
    detail_truncated: bool = False
    """True when ``details`` holds only the first ``len(details)`` of ``detail_total``."""


class IssueDetailPage(BaseModel):
    """One page of a single finding's evidence.

    The report ships a capped sample of every issue's ``details`` so the table
    has something to show immediately. This is what the dashboard fetches when
    the user actually pages through one of them, and it can reach the end of a
    list the report only sampled.
    """

    contract: str | None
    frequency: Frequency
    code: str
    total: int
    offset: int
    details: list[IssueDetail] = Field(default_factory=list)

    @property
    def has_more(self) -> bool:
        return self.offset + len(self.details) < self.total


class MissingTimestamp(BaseModel):
    """One instant with no bar."""

    model_config = ConfigDict(frozen=True)

    contract: str
    ts: dt.datetime
    label: str | None = None
    """Preferred over ``ts`` for display when a rule's unit is a date, not an instant."""
    classification: str
    """Which kind of absence this came from: ``intra_session``, ``extended`` or
    ``missing_session``. Expected breaks (weekends, the daily halt) are never
    listed, so every row here is an absence the exchange calendar does not explain."""


class MissingTimestampPage(BaseModel):
    """One page of the flat missing-timestamp listing.

    Paged rather than capped: a single thin contract is short six figures of
    bars, and "show every missing timestamp" is only honest if the client can
    walk to the end of the list.
    """

    contract: str | None
    frequency: Frequency
    expected_interval_s: int | None = None
    """The inferred bar interval the absences were measured against. Minute only."""
    total: int
    offset: int
    timestamps: list[MissingTimestamp] = Field(default_factory=list)

    @property
    def has_more(self) -> bool:
        return self.offset + len(self.timestamps) < self.total


class QualityReport(BaseModel):
    contract: str | None
    frequency: Frequency
    start: dt.date | None
    end: dt.date | None
    bars_checked: int
    issues: list[QualityIssue] = Field(default_factory=list)

    def by_severity(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for issue in self.issues:
            out[issue.severity] = out.get(issue.severity, 0) + issue.count
        return out

    def errors(self) -> list[QualityIssue]:
        return [i for i in self.issues if i.severity is Severity.ERROR]
