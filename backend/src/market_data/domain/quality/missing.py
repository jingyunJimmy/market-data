"""Expanding gaps into the individual timestamps that are absent.

The quality report answers "how bad is it" -- a count of gaps and the ranges
they span. This module answers the flatter question the dashboard asks: *which
instants are missing*, one row each.

The two are deliberately separate. A gap is a compact object (two endpoints and
a length) and there are thousands of them; the timestamps inside them number in
the hundreds of thousands for a single thin contract. Folding that into the
report would make the report heavier than the bars it describes, so the listing
is its own endpoint with its own cap and its own paging.

What counts as missing
----------------------
Only absences the exchange calendar does not explain. Weekends and the daily
halt are not missing data, and listing them would bury the real holes under a
quarter of a million rows that say "the market was shut". That is
``MinuteGaps.unexpected`` -- intra-session plus extended gaps.

The classification itself is *not* re-derived here. It comes from
:func:`~market_data.domain.quality.checks.gaps.classify_minute_gaps`, the same
function the report's gap rule uses, so the listing can never disagree with the
finding it drills into.
"""

from __future__ import annotations

import datetime as dt

import polars as pl

from market_data.config import Settings, get_settings
from market_data.domain.models import Frequency, MissingTimestamp, MissingTimestampPage
from market_data.domain.quality.checks.gaps import classify_minute_gaps, missing_sessions


def missing_timestamps(
    bars: pl.DataFrame,
    *,
    contract: str | None,
    frequency: Frequency,
    settings: Settings | None = None,
    limit: int = 2000,
    offset: int = 0,
) -> MissingTimestampPage:
    """Every instant with no bar, as one flat chronological page.

    ``offset``/``limit`` page through the whole set rather than truncating it:
    "display all the missing timestamps" is only true if the client can reach
    the end, and a cap alone would silently stop at the first few thousand.
    """
    settings = settings or get_settings()
    if bars.is_empty():
        return MissingTimestampPage(
            contract=contract, frequency=frequency, total=0, offset=offset, timestamps=[]
        )

    if frequency is Frequency.DAILY:
        frame, interval = _missing_daily(bars), None
    else:
        frame, interval = _missing_minute(bars, settings)

    total = frame.height
    page = frame.slice(offset, limit)
    return MissingTimestampPage(
        contract=contract,
        frequency=frequency,
        expected_interval_s=int(interval) if interval else None,
        total=total,
        offset=offset,
        timestamps=[MissingTimestamp(**row) for row in page.iter_rows(named=True)],
    )


def _missing_daily(bars: pl.DataFrame) -> pl.DataFrame:
    """One row per absent business day, stamped at the UTC midnight a daily bar uses."""
    frames: list[pl.DataFrame] = []
    for contract, grp in bars.group_by("contract", maintain_order=True):
        name = str(contract[0] if isinstance(contract, tuple) else contract)
        missing = missing_sessions(grp).missing
        if not missing.len():
            continue
        frames.append(
            pl.DataFrame({"day": missing}).select(
                pl.lit(name).alias("contract"),
                pl.col("day").cast(pl.Datetime("us")).dt.replace_time_zone("UTC").alias("ts"),
                pl.col("day").cast(pl.String).alias("label"),
                pl.lit("missing_session").alias("classification"),
            )
        )
    return _concat(frames)


def _missing_minute(bars: pl.DataFrame, settings: Settings) -> tuple[pl.DataFrame, float | None]:
    """Every absent bar instant inside an unexplained gap.

    A gap sits *between* two bars, so the absent run is the grid from one
    interval after the last bar present to one interval before the next. The
    expansion is vectorised (``datetime_ranges`` builds a list column, exploded
    into rows) because a Python loop over six figures of instants would dominate
    the request.
    """
    frames: list[pl.DataFrame] = []
    interval: float | None = None
    for contract, grp in bars.group_by("contract", maintain_order=True):
        name = str(contract[0] if isinstance(contract, tuple) else contract)
        gaps = classify_minute_gaps(grp, tz=settings.source_minute_tz, mult=settings.gap_min_multiple)
        if gaps is None:
            continue
        interval = gaps.expected_interval_s
        step = dt.timedelta(seconds=gaps.expected_interval_s)
        holes = gaps.unexpected
        if holes.is_empty():
            continue
        expanded = (
            holes.select(
                pl.datetime_ranges(
                    pl.col("prev") + step,
                    pl.col("ts") - step,
                    interval=step,
                    closed="both",
                    time_unit="us",
                    time_zone="UTC",
                ).alias("ts"),
                # An intra-session hole is the interesting one; an extended gap
                # is more likely a holiday. Carried through so the UI can say
                # which, without re-running the classification client-side.
                pl.when(pl.col("gap_days") == 0)
                .then(pl.lit("intra_session"))
                .otherwise(pl.lit("extended"))
                .alias("classification"),
            )
            # empty_as_null=False: a gap shorter than two intervals produces an
            # empty range, and dropping that row is the right answer -- there is
            # no absent instant inside it. Passed explicitly because the Polars
            # default for this flag is changing.
            .explode("ts", empty_as_null=False)
            .drop_nulls("ts")
        )
        frames.append(
            expanded.select(
                pl.lit(name).alias("contract"),
                pl.col("ts"),
                pl.lit(None, dtype=pl.String).alias("label"),
                pl.col("classification"),
            )
        )
    return _concat(frames), interval


def _concat(frames: list[pl.DataFrame]) -> pl.DataFrame:
    if not frames:
        return pl.DataFrame(
            schema={
                "contract": pl.String,
                "ts": pl.Datetime("us", "UTC"),
                "label": pl.String,
                "classification": pl.String,
            }
        )
    return pl.concat(frames).sort("contract", "ts")
