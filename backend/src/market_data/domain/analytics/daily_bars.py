"""Daily OHLCV bars aggregated from intraday (or passed through from daily) data."""

from __future__ import annotations

import polars as pl

DAILY_BAR_SCHEMA = (
    "contract",
    "trading_date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "open_interest",
    "bar_count",
)


def daily_ohlcv(bars: pl.DataFrame) -> pl.DataFrame:
    """Collapse canonical bars to one row per ``(contract, trading_date)``.

    * open  -- first bar of the session (earliest ``ts``)
    * high  -- session max
    * low   -- session min
    * close -- last bar of the session (latest ``ts``)
    * volume / open_interest -- summed / last-observed
    * bar_count -- number of source bars (1 for already-daily input)

    Daily input passes straight through (one bar per session already).
    """
    if bars.is_empty():
        return pl.DataFrame(schema={c: bars.schema.get(c, pl.Null) for c in DAILY_BAR_SCHEMA})

    ordered = bars.sort("contract", "ts")
    return (
        ordered.group_by("contract", "trading_date")
        .agg(
            pl.col("open").drop_nulls().first().alias("open"),
            pl.col("high").max().alias("high"),
            pl.col("low").min().alias("low"),
            pl.col("close").drop_nulls().last().alias("close"),
            pl.col("volume").sum().alias("volume"),
            pl.col("open_interest").drop_nulls().last().alias("open_interest"),
            pl.len().alias("bar_count"),
        )
        .sort("contract", "trading_date")
    )
