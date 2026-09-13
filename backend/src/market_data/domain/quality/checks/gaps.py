"""Missing-timestamp / data-gap detection.

The dataset is full of *legitimate* absences -- overnight breaks, weekends,
holidays, and long stretches of thin trading before a contract becomes the
front month. Naively diffing timestamps would drown the user in false
positives, so gaps are classified and only unexplained intra-session gaps are
raised as warnings.

How a minute gap is graded, in one line: severity is decided by whether the
gap crosses a *calendar day boundary* in the exchange tz, not by its duration.

    same Chicago date          -> intra_session_gap   WARNING
    crosses midnight, <= 36h   -> session_break       INFO
    crosses midnight, > 36h    -> extended_gap        WARNING
    weekend shape              -> session_break       INFO

Daily gaps are simpler: every *weekday* between the first and last observed
session should carry a bar, and the ones that do not are reported.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from dataclasses import dataclass

import polars as pl

from market_data.domain.models import (
    Frequency,
    IssueCategory,
    IssueDetail,
    QualityIssue,
    Severity,
)
from market_data.domain.quality.details import date_detail, detail_fields
from market_data.domain.quality.rules import RuleContext

#: Seconds in a day. ``gap_s`` is measured in seconds (``.dt.total_seconds()``)
#: but the thresholds below are naturally expressed in days, so this is purely a
#: unit conversion. ``1.5 * _DAY_S`` (36h) is the line between "one break" and
#: "at least a whole session is missing" -- deliberately generous, because the
#: span from a session's last bar to the next session's first is well over the
#: exchange's own 60-minute halt.
_DAY_S = 86_400


def _active_start(grp: pl.DataFrame) -> pl.Series | None:
    """First instant worth checking: where the contract actually started trading.

    A futures contract is listed long before it attracts flow, and the quiet
    stretch before that is not a data gap. Everything earlier is excluded.

    Known weakness: the bar is "any volume at all", and a back-month contract
    picks up stray fills very early, so for a thin period this barely filters
    anything and ``intra_session_gap`` gets loud. On ESZ25 the estimated missing
    bars come out higher than the bar count itself for exactly this reason. A
    liquidity floor (first session above some daily volume) would cut the noise.
    """
    traded = grp.filter(pl.col("volume").fill_null(0) > 0)
    src = traded if traded.height else grp
    return src["ts"].min()


def _gap_details(frame: pl.DataFrame, expected: float, limit: int) -> list[IssueDetail]:
    """One evidence row per gap: the span of bars that should have been there.

    A gap sits *between* two bars, so the missing run starts one interval after
    the last bar present and ends one interval before the next one. For a gap of
    a single bar the two collapse to the same instant.

    Ranges rather than one row per absent minute: a thin contract can be short
    six figures of bars, and enumerating each one would bury the user in rows
    that say nothing the range does not. ``missing_bars`` carries the size.

    Sliced to ``limit`` here rather than after building every row -- the caller
    passes the real total to ``detail_fields`` so the reported figure is intact.
    """
    step = dt.timedelta(seconds=expected)
    rows = frame.select("prev", "ts", "gap_s").sort("prev").head(limit)
    return [
        IssueDetail(
            ts=r["prev"] + step,
            end_ts=r["ts"] - step,
            values={
                "missing_bars": int(r["gap_s"] / expected) - 1,
                "gap_minutes": round(r["gap_s"] / 60, 1),
            },
        )
        for r in rows.iter_rows(named=True)
    ]


@dataclass(frozen=True)
class MinuteGaps:
    """One contract's gaps, already sorted into the four buckets.

    Extracted from the rule so that anything else needing the same judgement --
    the missing-timestamp listing in `missing.py` -- shares it rather than
    re-deriving the thresholds and drifting away from what the report says.

    Each frame carries the ``prev``/``ts``/``gap_s`` columns of the bars either
    side of the gap. ``unexpected`` is the pair a user should care about:
    absences the exchange calendar does not explain.
    """

    expected_interval_s: float
    weekend: pl.DataFrame
    overnight: pl.DataFrame
    extended: pl.DataFrame
    intra: pl.DataFrame

    @property
    def unexpected(self) -> pl.DataFrame:
        """Intra-session plus extended gaps, chronological: genuinely absent data."""
        return pl.concat([self.intra, self.extended]).sort("prev")


def classify_minute_gaps(grp: pl.DataFrame, *, tz: str, mult: float) -> MinuteGaps | None:
    """Bucket one contract's inter-bar spacing. ``None`` when there is too little
    data to infer what "normal" looks like, or nothing exceeds the threshold."""
    start = _active_start(grp)
    s = (
        grp.select("ts", "volume")
        .unique(subset=["ts"])
        .filter(pl.col("ts") >= start)
        .sort("ts")
        .with_columns(prev=pl.col("ts").shift(1))
        .drop_nulls("prev")
    )
    if s.height < 10:
        return None
    s = s.with_columns(gap_s=(pl.col("ts") - pl.col("prev")).dt.total_seconds())
    # The bar interval is *inferred*, not assumed to be 60s: the most common
    # spacing in the data wins, so a 5-minute file is graded against 300s.
    expected = _modal_interval(s["gap_s"])
    if expected <= 0:
        return None

    s = s.with_columns(
        tc=pl.col("ts").dt.convert_time_zone(tz),
        pc=pl.col("prev").dt.convert_time_zone(tz),
    ).with_columns(
        gap_days=(pl.col("tc").dt.date() - pl.col("pc").dt.date()).dt.total_days(),
        prev_wd=pl.col("pc").dt.weekday(),
        ts_wd=pl.col("tc").dt.weekday(),
    )
    # Only spacing beyond `gap_min_multiple` x the inferred interval is even a
    # candidate; ordinary jitter in a thin book is not a gap.
    big = s.filter(pl.col("gap_s") > mult * expected)
    if big.is_empty():
        return None

    # Sort the candidates into four disjoint buckets. Note what actually drives
    # severity: `gap_days`, i.e. whether the gap crossed midnight in the exchange
    # tz. The 36h line only separates "extended" from the rest.
    #
    # weekend -- crossed a date boundary AND started Fri-Sun or landed on a
    # Monday. `prev_wd >= 5` is Friday, not Saturday, on purpose: the futures
    # week runs Sunday evening to Friday afternoon Chicago, so a normal weekend
    # gap begins on the Friday bar.
    #
    # KNOWN FALSE NEGATIVE, and the worse of the two on this page: the test is
    # pure calendar shape with NO ceiling on duration, and `extended` below is
    # computed from `rest`, which this bucket has already been subtracted from.
    # So an outage of any length that happens to start on a Friday or resume on
    # a Monday can never reach `extended`. On ESZ25 that swallows a 25-day hole
    # (2024-01-18 -> 2024-02-12, ~36.5k absent bars) and reports it as an
    # expected weekend break. A real weekend is Friday 16:00 to Sunday 17:00
    # Chicago, about 49 hours, so a ceiling near three days would let anything
    # longer fall through to `extended` where it belongs.
    is_weekend = (pl.col("gap_days") >= 1) & ((pl.col("prev_wd") >= 5) | (pl.col("ts_wd") == 1))
    weekend = big.filter(is_weekend)
    rest = big.filter(~is_weekend)
    # overnight -- crossed a date boundary on a weekday, under 36h. Treated as an
    # expected break and reported only at INFO.
    #
    # KNOWN FALSE NEGATIVE: this bucket's name does not match how these products
    # trade. The real daily halt is 16:00-17:00 Chicago, which does NOT cross
    # midnight, so it lands in `intra` below. A gap that *does* cross midnight on
    # a continuously-traded contract usually means a real outage -- yet it is
    # demoted to INFO here as long as it stays under 36h. Measured on ESZ25: 106
    # gaps land here, including 11:49 -> 11:55 the next day (24.1h) and 06:12 ->
    # 13:53 the next day (31.7h). Both are outages reported as "expected session
    # break". The fix is a much tighter ceiling on this bucket, not the 36h one.
    overnight = rest.filter((pl.col("gap_days") >= 1) & (pl.col("gap_s") <= 1.5 * _DAY_S))
    # extended -- more than 36h and not a weekend: a holiday or an outage.
    extended = rest.filter(pl.col("gap_s") > 1.5 * _DAY_S)
    # intra -- inside one calendar day. The only bucket that represents a genuine
    # hole in a live session, so it is the primary WARNING.
    # The 36h clause is dead weight: same-date implies under 24h already.
    intra = rest.filter((pl.col("gap_days") == 0) & (pl.col("gap_s") <= 1.5 * _DAY_S))
    return MinuteGaps(
        expected_interval_s=expected,
        weekend=weekend,
        overnight=overnight,
        extended=extended,
        intra=intra,
    )


@dataclass(frozen=True)
class MissingSessions:
    """Absent sessions plus the window they were looked for in.

    The window matters to the wording: "2 business days with no bar between X
    and Y" says what was searched, which a bare list of dates does not.
    """

    missing: pl.Series
    first: dt.date | None = None
    last: dt.date | None = None


def missing_sessions(grp: pl.DataFrame) -> MissingSessions:
    """Business days between the first and last observed session with no bar.

    Shared with the missing-timestamp listing for the same reason as
    :func:`classify_minute_gaps`. Returns an empty series when the range is too
    short to say anything.

    Expect a bar on every *weekday*, not on every calendar day. Polars weekday is
    1=Mon..7=Sun, so `<= 5` keeps Mon-Fri and drops the weekend.

    Exchange holidays are NOT excluded -- there is no trading calendar in the
    project -- so Thanksgiving and Christmas are counted as missing. Callers say
    so out loud rather than overstating the finding. See "Known gaps" in
    docs/data-quality.md.
    """
    start = _active_start(grp)
    dates = grp.filter(pl.col("ts") >= start).select(pl.col("trading_date")).unique().sort("trading_date")
    if dates.height < 5:
        return MissingSessions(pl.Series("d", [], dtype=pl.Date))
    lo, hi = dates["trading_date"].min(), dates["trading_date"].max()
    business = pl.date_range(lo, hi, interval="1d", eager=True).alias("d")
    business = business.filter(business.dt.weekday() <= 5)
    return MissingSessions(business.filter(~business.is_in(dates["trading_date"])), lo, hi)


def timestamp_gaps(ctx: RuleContext) -> Iterable[QualityIssue]:
    if ctx.bars.is_empty():
        return []
    if ctx.frequency is Frequency.DAILY:
        return _daily_gaps(ctx)
    return _minute_gaps(ctx)


def _minute_gaps(ctx: RuleContext) -> list[QualityIssue]:
    tz = ctx.settings.source_minute_tz
    mult = ctx.settings.gap_min_multiple
    limit = ctx.settings.issue_detail_limit
    out: list[QualityIssue] = []

    for contract, grp in ctx.bars.group_by("contract", maintain_order=True):
        name = str(contract[0] if isinstance(contract, tuple) else contract)
        gaps = classify_minute_gaps(grp, tz=tz, mult=mult)
        if gaps is None:
            continue
        expected = gaps.expected_interval_s
        intra, extended = gaps.intra, gaps.extended

        if intra.height:
            missing = int(((intra["gap_s"] / expected) - 1).sum())
            out.append(
                QualityIssue(
                    category=IssueCategory.GAP,
                    code="intra_session_gap",
                    severity=Severity.WARNING,
                    contract=name,
                    frequency=ctx.frequency,
                    message=(
                        f"{intra.height} intra-session gaps (~{missing} missing "
                        f"{int(expected)}s bars) for {name}"
                    ),
                    count=int(intra.height),
                    start_ts=intra["prev"].min(),
                    end_ts=intra["ts"].max(),
                    context={
                        "expected_interval_s": int(expected),
                        "largest_gap_s": int(intra["gap_s"].max()),
                        "estimated_missing_bars": missing,
                    },
                    **detail_fields(_gap_details(intra, expected, limit), limit, total=intra.height),
                )
            )
        if extended.height:
            out.append(
                QualityIssue(
                    category=IssueCategory.GAP,
                    code="extended_gap",
                    severity=Severity.WARNING,
                    contract=name,
                    frequency=ctx.frequency,
                    message=f"{extended.height} multi-day gaps for {name} (holiday or data outage)",
                    count=int(extended.height),
                    start_ts=extended["prev"].min(),
                    end_ts=extended["ts"].max(),
                    context={"largest_gap_days": round(extended["gap_s"].max() / _DAY_S, 1)},
                    **detail_fields(_gap_details(extended, expected, limit), limit, total=extended.height),
                )
            )
        breaks = gaps.weekend.height + gaps.overnight.height
        if breaks:
            # Listed too, even though this is INFO. "We saw these and considered
            # them normal" is only checkable if the user can see which ones --
            # and it is where the misclassified outages noted above surface.
            recognised = pl.concat([gaps.weekend, gaps.overnight]).sort("prev")
            out.append(
                QualityIssue(
                    category=IssueCategory.GAP,
                    code="session_break",
                    severity=Severity.INFO,
                    contract=name,
                    frequency=ctx.frequency,
                    message=f"{breaks} expected session breaks (overnight/weekend) for {name}",
                    count=int(breaks),
                    context={
                        "weekend": int(gaps.weekend.height),
                        "overnight": int(gaps.overnight.height),
                    },
                    **detail_fields(_gap_details(recognised, expected, limit), limit, total=breaks),
                )
            )
    return out


def _daily_gaps(ctx: RuleContext) -> list[QualityIssue]:
    limit = ctx.settings.issue_detail_limit
    out: list[QualityIssue] = []
    for contract, grp in ctx.bars.group_by("contract", maintain_order=True):
        name = str(contract[0] if isinstance(contract, tuple) else contract)
        sessions = missing_sessions(grp)
        missing, lo, hi = sessions.missing, sessions.first, sessions.last
        if not missing.len():
            continue
        out.append(
            QualityIssue(
                category=IssueCategory.GAP,
                code="missing_sessions",
                severity=Severity.WARNING,
                contract=name,
                frequency=ctx.frequency,
                message=(
                    f"{missing.len()} business days with no bar for {name} "
                    f"between {lo} and {hi} (some are exchange holidays)"
                ),
                count=int(missing.len()),
                # The bounds of the window searched, not of a single occurrence:
                # the per-session dates are in `details`.
                start_ts=dt.datetime.combine(lo, dt.time(), tzinfo=dt.UTC) if lo else None,
                end_ts=dt.datetime.combine(hi, dt.time(), tzinfo=dt.UTC) if hi else None,
                context={"examples": [str(d) for d in missing.head(5).to_list()]},
                # Every absent session, by date. Unlike the minute path this is
                # genuinely one row per occurrence: a range holds at most a few
                # hundred business days, so there is nothing to fold.
                **detail_fields(
                    [date_detail(d) for d in missing.head(limit).to_list()],
                    limit,
                    total=int(missing.len()),
                ),
            )
        )
    return out


def _modal_interval(gap_s: pl.Series) -> float:
    """The most common positive spacing between consecutive bars.

    The mode, not the mean or the minimum: a handful of huge overnight gaps
    would drag an average away from the real bar size, and the minimum would be
    distorted by any single duplicate-ish timestamp.

    It does mean systematic sparseness cancels itself out. If a contract only
    prints every other minute, the mode becomes 120s and that thinness stops
    being visible as missing data.
    """
    positive = gap_s.filter(gap_s > 0)
    if positive.is_empty():
        return 0.0
    counts = positive.value_counts(sort=True)
    return float(counts.row(0)[0])
