"""Step 1: summarise a quality report into distributions a reader can reason about.

A finding says *how many*. Whether it is a pattern depends on *where* and
*when*: 9,500 gaps spread evenly across a session mean a thin book, 9,500 gaps
all starting at 16:00 mean the daily halt. This module computes those shapes
so the model never has to read raw occurrences -- and so that a reader can
check every figure it quotes against a number the code produced.

Pure: no I/O, no model calls. Given the same report it returns the same pack.

The statistics are taken from each issue's ``details``, so the report must be
built with a detail cap far above the dashboard's (``insights_max_occurrences``,
not ``issue_detail_limit``). ``Evidence.analysed`` records how many rows were
actually read, so a capped distribution never passes for a complete one.

Worked example
--------------
An ``intra_session_gap`` issue for ESZ25 (minute bars, 5 trading days scanned):
the 16:00 CT daily maintenance halt on every one of Mon-Fri (60 min each, 59
missing bars) plus one unrelated 5-minute gap on Wednesday at 09:30 CT::

    issue = QualityIssue(
        code="intra_session_gap", contract="ESZ25", frequency=MINUTE, count=6,
        context={"expected_interval_s": 60},
        details=[
            # Mon-Fri, each at UTC 22:00 (=16:00 CT): gap_minutes=60, missing_bars=59
            # Wed only, extra row at UTC 15:30 (=09:30 CT): gap_minutes=5, missing_bars=4
        ],
    )
    build_evidence_pack(
        QualityReport(contract="ESZ25", bars_checked=1500, issues=[issue]),
        trading_days=5, settings=Settings(),
    )

produces this ``Evidence`` (id ``E1``)::

    occurrences=6, analysed=6, per_1k_bars=4.0                  # 6 * 1000 / 1500
    first_seen=Mon 22:00Z, last_seen=Fri 22:00Z                 # min/max ts
    expected_interval_s=60                                      # from issue.context
    days_affected=5, days_affected_share=1.0                     # 5 distinct days / 5 scanned
    top_hours=[{hour:16, count:5, share:0.833},                 # UTC->CT hour of each ts
               {hour:9,  count:1, share:0.167}]
    weekday_counts={Mon:1, Tue:1, Wed:2, Thu:1, Fri:1}           # Wed has 2 rows, one date
    median_duration_min=60.0, p90_duration_min=60.0              # [5,60,60,60,60,60] sorted
    period="week", period_counts=[{start:2025-01-06, count:6}]  # 4-day span -> weekly buckets

The distribution tells the story without a human reading six rows: concentrated
at hour 16, on every trading day, at a duration that never varies -- the daily
halt, not a defect. The one Wednesday gap barely moves a median drawn from six
points, and disappears entirely once real data supplies thousands -- which is
the point of reasoning from shape rather than from a raw occurrence count.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable

import polars as pl

from market_data.config import Settings
from market_data.domain.insights.catalogue import TUNABLE_SETTINGS
from market_data.domain.insights.models import (
    Evidence,
    EvidencePack,
    HourShare,
    InsightScope,
    PeriodCount,
)
from market_data.domain.models import Frequency, QualityIssue, QualityReport

#: How soon after a gap an occurrence must land to count as "after a gap".
#: Five bars on minute data: long enough to catch the first prints of a
#: resumed session, short enough not to swallow the rest of it.
AFTER_GAP_WINDOW_MIN = 5

GAP_CODES = frozenset({"intra_session_gap", "extended_gap", "session_break"})

#: The per-occurrence figure that says how extreme a hit was, by finding.
MAGNITUDE_METRICS = {"price_return_outlier": "mad_score", "volume_spike": "x_median"}

_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_TOP_HOURS = 3

_OCCURRENCE_SCHEMA = {
    "ts": pl.Datetime("us", "UTC"),
    "end_ts": pl.Datetime("us", "UTC"),
    "duration_min": pl.Float64,
    "magnitude": pl.Float64,
    "missing_bars": pl.Float64,
}


def build_evidence_pack(report: QualityReport, *, trading_days: int, settings: Settings) -> EvidencePack:
    """One :class:`Evidence` per non-empty finding, ids in report order.

    ``trading_days`` is the number of distinct sessions in the bars checked:
    it is the denominator of ``days_affected_share``, and the report does not
    carry it.
    """
    tz = settings.daily_bar_tz
    minute = report.frequency is Frequency.MINUTE
    issues = [i for i in report.issues if i.count > 0]
    intervals = _intervals(issues)
    resumes = _gap_resumes(issues, intervals) if minute else {}

    evidence = [
        _evidence(
            f"E{n}",
            issue,
            tz=tz,
            minute=minute,
            trading_days=trading_days,
            bars_checked=report.bars_checked,
            interval_s=intervals.get(issue.contract) if issue.code in GAP_CODES else None,
            resumes=None if issue.code in GAP_CODES else resumes.get(issue.contract),
        )
        for n, issue in enumerate(issues, start=1)
    ]
    return EvidencePack(
        scope=InsightScope(
            contract=report.contract,
            frequency=report.frequency,
            start=report.start,
            end=report.end,
            bars_checked=report.bars_checked,
            trading_days=trading_days,
            timezone=tz,
            after_gap_window_min=AFTER_GAP_WINDOW_MIN,
        ),
        thresholds={name: float(getattr(settings, name)) for name in TUNABLE_SETTINGS},
        evidence=evidence,
    )


def _evidence(
    eid: str,
    issue: QualityIssue,
    *,
    tz: str,
    minute: bool,
    trading_days: int,
    bars_checked: int,
    interval_s: int | None,
    resumes: pl.DataFrame | None,
) -> Evidence:
    """One issue's row-level ``details`` reduced to distribution statistics.

    Example: an ``intra_session_gap`` issue for ESZ25 (minute bars, 5 trading
    days scanned) with 6 occurrences -- the 16:00 CT daily maintenance halt on
    every one of Mon-Fri (60 min each) plus one unrelated 5-minute gap on
    Wednesday at 09:30 CT -- comes out roughly as::

        Evidence(
            id="E1", contract="ESZ25", occurrences=6, analysed=6, per_1k_bars=4.0,
            first_seen=Mon 22:00Z, last_seen=Fri 22:00Z, expected_interval_s=60,
            days_affected=5, days_affected_share=1.0,                  # 5 of 5 days scanned
            top_hours=[{hour:16, count:5, share:0.833},                # UTC->CT hour of each ts
                       {hour:9,  count:1, share:0.167}],
            weekday_counts={Mon:1, Tue:1, Wed:2, Thu:1, Fri:1},         # Wed has 2 rows, 1 date
            median_duration_min=60.0, p90_duration_min=60.0,           # [5,60,60,60,60,60] sorted
            period="week", period_counts=[{start:2025-01-06, count:6}],
        )

    See the module docstring for the full input and the reasoning behind it.
    """
    occ = _occurrences(issue)
    base = {
        "id": eid,  # E1, E2, ... -- the handle patterns cite.
        "contract": issue.contract,
        "code": issue.code,  # The check that raised this, e.g. "intra_session_gap".
        "category": issue.category,  # The check's broad grouping (gap, outlier, duplicate, ...).
        "severity": issue.severity,  # As the report assigned it; unrelated to how much of a pattern this is.
        # The report's own count -- can exceed `analysed` when a per-issue cap
        # cut `issue.details` before this pack was built.
        "occurrences": issue.count,
        # Rows this Evidence's statistics are actually computed from; zero for
        # a rule that never enumerated its occurrences at all.
        "analysed": occ.height,
        # `occurrences` normalised by how much data was scanned, so findings
        # from differently-sized scans are comparable.
        "per_1k_bars": round(issue.count * 1000 / bars_checked, 2) if bars_checked else 0.0,
        # Some rules state their own first/last instant on the issue; fall
        # back to the detail rows' own min/max (UTC) when they do not.
        "first_seen": issue.start_ts or (occ["ts"].min() if occ.height else None),
        "last_seen": issue.end_ts or (occ["ts"].max() if occ.height else None),
        # The bar interval a gap was measured against; None for non-gap findings.
        "expected_interval_s": interval_s,
    }
    if occ.is_empty():
        return Evidence(**base)

    # A minute occurrence belongs to the exchange-time date and hour it happened
    # in. A daily one is a session date already stamped at UTC midnight, and
    # shifting it into Chicago would move it onto the previous evening.
    if minute:
        occ = occ.with_columns(local=pl.col("ts").dt.convert_time_zone(tz)).with_columns(
            day=pl.col("local").dt.date(), hour=pl.col("local").dt.hour()
        )
    else:
        occ = occ.with_columns(day=pl.col("ts").dt.date())

    days = occ["day"].n_unique()
    stats: dict[str, object] = {
        "days_affected": days,
        # Only meaningful when the occurrences fall on sessions that were
        # checked. A missing session is by definition not one of them.
        "days_affected_share": (
            round(days / trading_days, 3)
            if trading_days and days <= trading_days and issue.code != "missing_sessions"
            else None
        ),
        "weekday_counts": _weekday_counts(occ),
        **_durations(occ),
        **_magnitude(occ, issue.code),
        **_periods(occ),
    }
    if minute:
        stats["top_hours"] = _top_hours(occ)
        if resumes is not None and not resumes.is_empty():
            stats["share_after_gap"] = _share_after_gap(occ, resumes)
    return Evidence(**base, **stats)


def _occurrences(issue: QualityIssue) -> pl.DataFrame:
    """The finding's detail rows as a frame, keeping only the figures used here."""
    metric = MAGNITUDE_METRICS.get(issue.code)
    rows = [d for d in issue.details if d.ts is not None]
    return pl.DataFrame(
        {
            "ts": [d.ts for d in rows],
            "end_ts": [d.end_ts for d in rows],
            "duration_min": [_number(d.values.get("gap_minutes")) for d in rows],
            "magnitude": [_number(d.values.get(metric)) if metric else None for d in rows],
            "missing_bars": [_number(d.values.get("missing_bars")) for d in rows],
        },
        schema=_OCCURRENCE_SCHEMA,
    )


def _top_hours(occ: pl.DataFrame) -> list[HourShare]:
    # Ties broken by the earlier hour, so the pack is deterministic.
    counts = occ.group_by("hour").len().sort(["len", "hour"], descending=[True, False]).head(_TOP_HOURS)
    return [
        HourShare(hour=int(hour), count=int(n), share=round(n / occ.height, 3))
        for hour, n in counts.iter_rows()
    ]


def _weekday_counts(occ: pl.DataFrame) -> dict[str, int]:
    counts = occ.group_by(pl.col("day").dt.weekday().alias("wd")).len().sort("wd")
    return {_WEEKDAYS[int(wd) - 1]: int(n) for wd, n in counts.iter_rows()}


def _durations(occ: pl.DataFrame) -> dict[str, object]:
    minutes = occ["duration_min"].drop_nulls()
    if minutes.is_empty():
        return {}
    return {
        "median_duration_min": round(float(minutes.median()), 1),
        "p90_duration_min": round(float(minutes.quantile(0.9, interpolation="nearest")), 1),
    }


def _magnitude(occ: pl.DataFrame, code: str) -> dict[str, object]:
    values = occ["magnitude"].drop_nulls()
    if values.is_empty():
        return {}
    return {"magnitude_metric": MAGNITUDE_METRICS[code], "max_magnitude": round(float(values.max()), 1)}


def _periods(occ: pl.DataFrame) -> dict[str, object]:
    """Counts per week, month or quarter, whichever keeps the series short.

    A trend is read from a couple of dozen buckets. Weekly over two years
    would be a hundred numbers nobody reads, and would cost an LLM tokens.
    """
    span = int(occ.select((pl.col("day").max() - pl.col("day").min()).dt.total_days()).item())
    if span <= 26 * 7:
        period, every = "week", "1w"
    elif span <= 3 * 366:
        period, every = "month", "1mo"
    else:
        period, every = "quarter", "3mo"
    counts = occ.group_by(pl.col("day").dt.truncate(every).alias("start")).len().sort("start")
    return {
        "period": period,
        "period_counts": [PeriodCount(start=start, count=int(n)) for start, n in counts.iter_rows()],
    }


def _share_after_gap(occ: pl.DataFrame, resumes: pl.DataFrame) -> float:
    """Share of occurrences within the window after the nearest preceding gap ended.

    As-of join on the resume instant: each occurrence finds the latest gap that
    ended at or before it, and counts if that was recent enough.
    """
    joined = (
        occ.select("ts")
        .sort("ts")
        .join_asof(resumes, left_on="ts", right_on="resume", strategy="backward")
        .filter((pl.col("ts") - pl.col("resume")) <= dt.timedelta(minutes=AFTER_GAP_WINDOW_MIN))
    )
    return round(joined.height / occ.height, 3)


def _intervals(issues: Iterable[QualityIssue]) -> dict[str, int]:
    """The bar interval each contract's gaps were measured against.

    ``intra_session_gap`` states it outright. The other gap findings do not,
    but a gap detail carries both its length and its missing bars, and
    ``length / (missing + 1)`` is the interval.
    """
    out: dict[str, int] = {}
    for issue in issues:
        stated = issue.context.get("expected_interval_s")
        if isinstance(stated, int) and stated > 0:
            out[issue.contract] = stated
    for issue in issues:
        if issue.code not in GAP_CODES or issue.contract in out:
            continue
        for d in issue.details:
            minutes, missing = _number(d.values.get("gap_minutes")), _number(d.values.get("missing_bars"))
            if minutes and missing is not None:
                out[issue.contract] = round(minutes * 60 / (missing + 1))
                break
    return out


def _gap_resumes(issues: Iterable[QualityIssue], intervals: dict[str, int]) -> dict[str, pl.DataFrame]:
    """Per contract, the sorted instants at which bars resumed after a gap.

    A gap detail's ``end_ts`` is the last *missing* bar, so the bar that
    resumed the series is one interval later.
    """
    resumed: dict[str, list[dt.datetime]] = {}
    for issue in issues:
        step = intervals.get(issue.contract)
        if issue.code not in GAP_CODES or not step:
            continue
        resumed.setdefault(issue.contract, []).extend(
            d.end_ts + dt.timedelta(seconds=step) for d in issue.details if d.end_ts is not None
        )
    return {
        contract: pl.DataFrame({"resume": sorted(ts)}, schema={"resume": pl.Datetime("us", "UTC")})
        for contract, ts in resumed.items()
    }


def _number(value: object) -> float | None:
    # bool is an int subclass; a flag is not a figure.
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)
