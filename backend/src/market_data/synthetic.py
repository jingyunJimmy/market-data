"""Deterministic synthetic bar generation -- for demos, fixtures, and tests.

Kept in the package (not tests/) so ``scripts/make_fixtures.py`` and the test
suite share one source of truth for what "clean" and "dirty" data look like.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Iterator

import polars as pl

_CHICAGO = "America/Chicago"


def clean_minute_bars(
    *,
    contract: str = "CL_TEST",
    root: str = "CL",
    exchange: str = "NYMEX",
    start: dt.datetime = dt.datetime(2024, 3, 4, 9, 0),  # a normal Monday
    minutes: int = 390,
    seed: int = 7,
) -> pl.DataFrame:
    """A continuous run of 1-minute bars with a gentle random walk.

    ``start`` is naive Chicago wall-clock time, matching the real dataset's
    minute files (column ``timestamp_chicago_wall``).
    """
    rng = _lcg(seed)
    price = 78.0
    rows = []
    for i in range(minutes):
        drift = (next(rng) - 0.5) * 0.15
        o = price
        c = max(0.01, o + drift)
        hi = max(o, c) + next(rng) * 0.05
        lo = min(o, c) - next(rng) * 0.05
        vol = 50 + int(next(rng) * 400) + int(30 * math.sin(i / 20))
        ts = start + dt.timedelta(minutes=i)
        rows.append(
            {
                "contract_symbol": contract,
                "root": root,
                "exchange": exchange,
                "timestamp_chicago_wall": ts,
                "trading_date": start.date(),
                "open": round(o, 2),
                "high": round(hi, 2),
                "low": round(lo, 2),
                "close": round(c, 2),
                "volume": max(0, vol),
            }
        )
        price = c
    return pl.DataFrame(rows)


def clean_daily_bars(
    *,
    contract: str = "CL_TEST",
    root: str = "CL",
    exchange: str = "NYMEX",
    start: dt.date = dt.date(2024, 1, 1),
    days: int = 90,
    seed: int = 11,
) -> pl.DataFrame:
    rng = _lcg(seed)
    price = 75.0
    rows = []
    d = start
    for _ in range(days):
        if d.weekday() < 5:  # business days only
            o = price
            c = max(0.01, o + (next(rng) - 0.5) * 2)
            hi = max(o, c) + next(rng)
            lo = min(o, c) - next(rng)
            rows.append(
                {
                    "contract_symbol": contract,
                    "root": root,
                    "exchange": exchange,
                    "date": d,
                    "open": round(o, 2),
                    "high": round(hi, 2),
                    "low": round(lo, 2),
                    "close": round(c, 2),
                    "volume": 10_000 + int(next(rng) * 5_000),
                    "open_interest": 200_000,
                }
            )
            price = c
        d += dt.timedelta(days=1)
    return pl.DataFrame(rows)


def dirty_minute_bars(seed: int = 7) -> pl.DataFrame:
    """Clean minute bars with a known set of quality problems injected."""
    base = clean_minute_bars(minutes=120, seed=seed)

    # 1) drop a 20-minute block -> intra-session gap
    base = base.filter(
        ~pl.col("timestamp_chicago_wall").is_between(
            dt.datetime(2024, 3, 4, 10, 0), dt.datetime(2024, 3, 4, 10, 20)
        )
    )

    extras = pl.DataFrame(
        [
            # 2) exact duplicate of the first row
            base.row(0, named=True),
            base.row(0, named=True),
            # 3) conflicting duplicate: same ts as row 1, different close
            {**base.row(1, named=True), "close": base.row(1, named=True)["close"] + 5.0},
            # 4) high < low
            {**base.row(2, named=True), "high": 1.0, "low": 99.0},
            # 5) negative volume
            {**base.row(3, named=True), "volume": -10},
            # 6) non-positive price
            {**base.row(4, named=True), "open": 0.0},
        ],
        schema=base.schema,
    )
    out = pl.concat([base, extras], how="vertical")

    # 7) rows that must be *rejected* at ingest (string frame -> add as text)
    text = out.with_columns(pl.all().cast(pl.String))
    bad_rows = pl.DataFrame(
        [
            {**text.row(0, named=True), "timestamp_chicago_wall": "not-a-timestamp"},
            {**text.row(0, named=True), "close": "abc"},
            {**text.row(0, named=True), "open": None, "high": None, "low": None, "close": None},
        ],
        schema=text.schema,
    )
    return pl.concat([text, bad_rows], how="vertical")


def _lcg(seed: int) -> Iterator[float]:
    """Tiny deterministic PRNG in [0, 1) -- no numpy dependency, reproducible."""
    state = seed & 0xFFFFFFFF
    while True:
        state = (1_103_515_245 * state + 12_345) & 0x7FFFFFFF
        yield state / 0x7FFFFFFF
