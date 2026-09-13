"""Rolling, time-based VWAP.

VWAP over a trailing window ``W`` at bar ``t`` is::

    sum(typical_price_i * volume_i) / sum(volume_i)   for  t - W < ts_i <= t

with ``typical_price = (high + low + close) / 3``. Windows whose total volume is
zero (e.g. a run of settlement-only bars) yield a null VWAP rather than a
divide-by-zero; null volumes count as zero rather than poisoning the sum.

A partial window is computed, not withheld: the window is a time span, so at
the start of a series (or after any break longer than ``W``) it simply holds
fewer bars, and the first bar's VWAP is its own typical price. Requiring a full
window would blank the opening of every session, which is often what a user
came to look at. The cost is that early points rest on a smaller sample than
later ones without looking any different on a chart.

Absent bars are never filled in. A hole in the series just leaves fewer bars in
the windows that span it -- nothing is interpolated, carried forward, or nulled,
and the output carries no row at an instant that has no bar. So a gap quietly
shifts the weighting rather than announcing itself here; the quality layer is
what reports it (``intra_session_gap``, and the missing-timestamp listing).
Consumers plotting the result should break the line across a gap rather than
join the points either side, which would read as steady movement through it.

Rows whose timestamp could not be parsed never reach this function: ingestion
rejects them as ``unparseable_timestamp``, so ``ts`` is non-null here.

The output is exactly one row per input bar, in chronological order, even where
several bars share a timestamp.
"""

from __future__ import annotations

import polars as pl


def rolling_vwap(bars: pl.DataFrame, *, window_minutes: int = 15) -> pl.DataFrame:
    """Return ``contract, ts, typical_price, vwap`` -- one row per input bar."""
    out_schema = pl.Schema(
        {  # type: ignore[arg-type]
            "contract": pl.String,
            "ts": pl.Datetime("us", "UTC"),
            "typical_price": pl.Float64,
            "vwap": pl.Float64,
        }
    )
    if bars.is_empty():
        return pl.DataFrame(schema=out_schema)

    window = f"{window_minutes}m"
    return (
        bars.sort("contract", "ts")
        .with_columns(
            ((pl.col("high") + pl.col("low") + pl.col("close")) / 3).alias("typical_price"),
            pl.col("volume").fill_null(0).alias("_vol"),
        )
        .with_columns((pl.col("typical_price") * pl.col("_vol")).alias("_pv"))
        # Trailing time window (t - W, t], per contract.
        #
        # By time, not by bar count, so the window holds however many bars fall
        # inside the span -- fewer at the start of a series, across a gap, or
        # over a session break. There is no minimum sample: a partial window
        # produces a value rather than a null, and absent bars are not filled in
        # (see the module docstring). A break longer than W leaves a bar alone in
        # its own window, so its VWAP is its own typical price.
        #
        # rolling_sum_by is an *expression*, so it yields one value per input row
        # and every bar keeps its own typical_price alongside its window sums.
        # A frame-level .rolling(...).agg(...) would collapse the window and need
        # a join back on (contract, ts) to recover the bar's own price -- and
        # that join fans out into a cartesian product wherever a timestamp
        # carries conflicting duplicates, inflating the row count and pairing
        # one bar's price with another's VWAP.
        .with_columns(
            pl.col("_pv")
            .rolling_sum_by("ts", window_size=window, closed="right")
            .over("contract")
            .alias("_pv_sum"),
            pl.col("_vol")
            .rolling_sum_by("ts", window_size=window, closed="right")
            .over("contract")
            .alias("_window_volume"),
        )
        # The guard is on the *window* sum, not the bar: a single zero-volume bar
        # inside an otherwise traded window still gets a real VWAP, and only a
        # window with nothing behind it -- the first bars of a series, a run of
        # settlement-only bars -- comes back null.
        #
        # `> 0` rather than `!= 0` because ingestion keeps negative volume (a bar
        # with one is still a bar; the quality layer reports it). So a window
        # summing to zero or below is meaningless as a denominator and is nulled.
        # A window whose sum stays positive despite a negative member still
        # divides, and can land outside the window's price range -- bad input
        # producing an odd point, which `negative_volume` is what flags.
        .with_columns(
            pl.when(pl.col("_window_volume") > 0)
            .then(pl.col("_pv_sum") / pl.col("_window_volume"))
            .otherwise(None)
            .alias("vwap")
        )
        .select("contract", "ts", "typical_price", "vwap")
        # Chronological order is part of the contract: AnalyticsService.vwap
        # takes .tail(limit) to mean "the most recent points".
        .sort("contract", "ts")
    )
