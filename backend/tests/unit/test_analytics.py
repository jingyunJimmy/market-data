"""Session aggregation and rolling VWAP.

Two computations the dashboard shows as numbers a user would act on, so the
tests check the arithmetic itself rather than the shape of the output. Where a
value is checkable independently it is recomputed the slow, obvious way in
Python and compared, rather than re-expressed in the same Polars idiom the
implementation uses.
"""

from __future__ import annotations

import datetime as dt

import polars as pl
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from market_data.domain.analytics import daily_ohlcv, rolling_vwap
from market_data.domain.models import CANONICAL_SCHEMA


def test_daily_ohlcv_aggregates_session(canonical_bars: pl.DataFrame):
    """Each field aggregates its own way: first, last, max, min, sum, count.

    The fixture's high and low sit mid-run, so a `high` taken from the first or
    last bar would fail here rather than coincidentally agree.
    """
    daily = daily_ohlcv(canonical_bars)
    assert daily.height == 1
    row = daily.row(0, named=True)
    assert row["open"] == 10.0  # first bar
    assert row["close"] == 9.8  # last bar
    assert row["high"] == 10.5
    assert row["low"] == 9.6
    assert row["volume"] == 500
    assert row["bar_count"] == 5


def test_daily_ohlcv_empty():
    """No bars aggregates to no rows, not to a row of nulls."""
    empty = pl.DataFrame(schema=CANONICAL_SCHEMA)
    assert daily_ohlcv(empty).is_empty()


# --------------------------------------------------------------------------- #
# Daily aggregation edge cases
#
# The happy path above is one contract on one session. Everything below is a
# shape the fixtures do not happen to produce: several groups at once, input
# that is not in order, a session that crosses midnight UTC, and holes in the
# columns that are summed or carried forward.
# --------------------------------------------------------------------------- #


def _session_bars(rows: list[dict]) -> pl.DataFrame:
    """Canonical bars from partial rows. `ts` is required; the rest default.

    `trading_date` follows the bar's own UTC date unless a row overrides it,
    which is how the cross-midnight case says "these belong to one session".
    """
    filled = []
    for i, r in enumerate(rows):
        filled.append(
            {
                "contract": "X",
                "root": "X",
                "exchange": "E",
                "frequency": "minute",
                "trading_date": r["ts"].date(),
                "open": 10.0,
                "high": 11.0,
                "low": 9.0,
                "close": 10.5,
                "volume": 100,
                "open_interest": None,
                "source_file": "m",
                "row_hash": f"h{i}",
                "ingested_at": r["ts"],
                **r,
            }
        )
    return pl.DataFrame(filled, schema=CANONICAL_SCHEMA)


def _at(day: int, hour: int = 15, minute: int = 0) -> dt.datetime:
    """A UTC instant in March 2024, short enough to read inline."""
    return dt.datetime(2024, 3, day, hour, minute, tzinfo=dt.UTC)


def test_daily_ohlcv_groups_by_contract_and_session():
    """Each (contract, session) is its own bar, and the output is sorted.

    With more than one group, an open or close taken globally rather than
    per group would show up immediately -- contract B's Tuesday would open at
    contract A's Monday price.
    """
    bars = _session_bars(
        [
            {"ts": _at(5), "contract": "B", "open": 5.0, "close": 5.5},
            {"ts": _at(4), "contract": "B", "open": 4.0, "close": 4.5},
            {"ts": _at(4), "contract": "A", "open": 1.0, "close": 1.5},
        ]
    )

    out = daily_ohlcv(bars)

    assert out.select("contract", "trading_date", "open", "close", "bar_count").to_dicts() == [
        {
            "contract": "A",
            "trading_date": dt.date(2024, 3, 4),
            "open": 1.0,
            "close": 1.5,
            "bar_count": 1,
        },
        {
            "contract": "B",
            "trading_date": dt.date(2024, 3, 4),
            "open": 4.0,
            "close": 4.5,
            "bar_count": 1,
        },
        {
            "contract": "B",
            "trading_date": dt.date(2024, 3, 5),
            "open": 5.0,
            "close": 5.5,
            "bar_count": 1,
        },
    ]


def test_daily_ohlcv_reads_open_and_close_from_the_clock_not_the_row_order():
    """Unordered input still opens on the earliest bar and closes on the latest.

    Nothing guarantees a frame arrives sorted -- concatenated sources, a
    re-ingested correction -- so the session's first and last are decided by
    `ts` rather than by position.
    """
    bars = _session_bars(
        [
            {"ts": _at(4, 15, 2), "open": 3.0, "close": 3.5},
            {"ts": _at(4, 15, 0), "open": 1.0, "close": 1.5},
            {"ts": _at(4, 15, 1), "open": 2.0, "close": 2.5},
        ]
    )

    out = daily_ohlcv(bars)

    assert out.height == 1
    assert (out["open"][0], out["close"][0]) == (1.0, 3.5)


def test_a_session_crossing_midnight_utc_stays_one_bar():
    """The grouping key is the trading date, which is why this is one session.

    A futures session runs through midnight UTC, so these two bars belong to
    the same trading day even though their UTC dates differ. Grouping on the
    UTC date instead would split the session in two and give each half a
    truncated open and close.
    """
    session = dt.date(2024, 3, 4)
    bars = _session_bars(
        [
            {"ts": _at(4, 23, 0), "trading_date": session, "open": 1.0, "close": 1.5},
            {"ts": _at(5, 1, 0), "trading_date": session, "open": 2.0, "close": 2.5},
        ]
    )

    out = daily_ohlcv(bars)

    assert out.height == 1
    assert out["trading_date"][0] == session
    # Close comes from the later instant, which falls on the next UTC date.
    assert (out["open"][0], out["close"][0]) == (1.0, 2.5)
    assert out["bar_count"][0] == 2


def test_volume_sums_over_the_bars_that_have_one():
    """A missing volume is skipped rather than nulling the session's total.

    Note the second case: a session with no volume data anywhere reports zero,
    which reads the same as a session that genuinely did not trade. Nothing
    downstream distinguishes them today.
    """
    partial = _session_bars([{"ts": _at(4, 15, 0), "volume": None}, {"ts": _at(4, 15, 1), "volume": 50}])
    assert daily_ohlcv(partial)["volume"][0] == 50

    none_at_all = _session_bars(
        [{"ts": _at(4, 15, 0), "volume": None}, {"ts": _at(4, 15, 1), "volume": None}]
    )
    assert daily_ohlcv(none_at_all)["volume"][0] == 0


def test_open_interest_is_the_last_one_actually_reported():
    """Open interest is carried forward, not summed: it is a level, not a flow.

    Summing it would multiply the position count by the number of bars. The
    last *observed* value wins, so a trailing bar without one does not erase it.
    """
    bars = _session_bars(
        [{"ts": _at(4, 15, 0), "open_interest": 500}, {"ts": _at(4, 15, 1), "open_interest": None}]
    )

    assert daily_ohlcv(bars)["open_interest"][0] == 500


def test_open_and_close_fall_through_to_the_nearest_bar_that_has_a_price():
    """A null price at either end does not become the session's open or close.

    Ingestion rejects a bar with no OHLC, so this cannot arrive from a file --
    but the function is also called on hand-assembled frames, and a null open
    would otherwise propagate into the daily bar.
    """
    bars = _session_bars(
        [
            {"ts": _at(4, 15, 0), "open": None, "close": 1.5},
            {"ts": _at(4, 15, 1), "open": 7.0, "close": None},
        ]
    )

    out = daily_ohlcv(bars)

    assert out["open"][0] == 7.0
    assert out["close"][0] == 1.5


def test_already_daily_input_passes_through_one_bar_per_session():
    """Daily bars are collapsed against themselves, so nothing changes.

    The same endpoint serves both frequencies, and a daily file must not be
    re-aggregated into something different from what it already said.
    """
    bars = _session_bars(
        [
            {"ts": _at(4, 0, 0), "frequency": "daily", "open": 1.0, "close": 1.5, "open_interest": 900},
            {"ts": _at(5, 0, 0), "frequency": "daily", "open": 2.0, "close": 2.5, "open_interest": 950},
        ]
    )

    out = daily_ohlcv(bars)

    assert out["bar_count"].to_list() == [1, 1]
    assert out["open"].to_list() == [1.0, 2.0]
    assert out["close"].to_list() == [1.5, 2.5]
    assert out["open_interest"].to_list() == [900, 950]


def test_rolling_vwap_empty_keeps_the_output_schema():
    """An empty result still has to be concat-able and serialisable."""
    out = rolling_vwap(pl.DataFrame(schema=CANONICAL_SCHEMA))
    assert out.is_empty()
    assert out.columns == ["contract", "ts", "typical_price", "vwap"]


def test_rolling_vwap_matches_independent_computation(canonical_bars: pl.DataFrame):
    """The oracle test: recompute VWAP in plain Python and compare bar by bar.

    Written from the definition -- typical price is (high + low + close) / 3,
    weighted by volume over a trailing window that is open at the left and
    closed at the right -- so it would catch a window that includes its own
    left edge or is off by one bar.

    Five bars against a three-minute window, so the first two are computed over
    a partial window. The oracle treats those the same way the implementation
    does: whatever bars fall in the span, with no minimum sample.
    """
    window = 3
    got = rolling_vwap(canonical_bars, window_minutes=window).sort("ts")

    rows = canonical_bars.sort("ts").to_dicts()
    for i, row in enumerate(rows):
        lo = row["ts"] - dt.timedelta(minutes=window)
        window_rows = [r for r in rows if lo < r["ts"] <= row["ts"]]
        pv = sum((r["high"] + r["low"] + r["close"]) / 3 * r["volume"] for r in window_rows)
        vol = sum(r["volume"] for r in window_rows)
        assert got["vwap"][i] == pytest.approx(pv / vol)


def test_rolling_vwap_zero_volume_is_null():
    """A window with no volume has no volume-weighted price.

    Null, not zero and not a crash: zero would plot as a price of 0 on the
    chart, which reads as a real print rather than as absent data.

    One bar, so the bar and its window are the same thing. The guard is
    actually on the window sum, which is why a zero-volume bar sitting inside a
    traded window still gets a real VWAP.
    """
    base = dt.datetime(2024, 3, 4, 15, 0, tzinfo=dt.UTC)
    bars = pl.DataFrame(
        [
            {
                "contract": "X",
                "root": "X",
                "exchange": "E",
                "frequency": "minute",
                "ts": base,
                "trading_date": base.date(),
                "open": 5.0,
                "high": 5.0,
                "low": 5.0,
                "close": 5.0,
                "volume": 0,
                "open_interest": None,
                "source_file": "m",
                "row_hash": "a",
                "ingested_at": base,
            }
        ],
        schema=CANONICAL_SCHEMA,
    )
    assert rolling_vwap(bars)["vwap"][0] is None


# --------------------------------------------------------------------------- #
# Gaps
#
# The window is a time span, so a hole in the series is not an error condition
# -- it just leaves fewer bars in the windows that cross it. Nothing is
# interpolated or carried forward, and no row is emitted for an absent bar.
# These pin that, because the behaviour is otherwise only written down.
# --------------------------------------------------------------------------- #


def _bar(minute: int, price: float, volume: int = 100) -> dict:
    """A canonical bar at `minute` past the fixture epoch, flat at `price`.

    Open, high, low and close are all `price`, so the typical price is `price`
    and a VWAP can be read off by hand.
    """
    base = dt.datetime(2024, 3, 4, 15, 0, tzinfo=dt.UTC)
    return {
        "contract": "X",
        "root": "X",
        "exchange": "E",
        "frequency": "minute",
        "ts": base + dt.timedelta(minutes=minute),
        "trading_date": base.date(),
        "open": price,
        "high": price,
        "low": price,
        "close": price,
        "volume": volume,
        "open_interest": None,
        "source_file": "m",
        "row_hash": f"h{minute}",
        "ingested_at": base,
    }


def test_a_gap_shorter_than_the_window_just_leaves_fewer_bars_in_it():
    """Bars missing mid-series are neither filled in nor cause for a null.

    Minutes 2 and 3 are absent. At 15:04 the five-minute window should hold the
    three bars that exist rather than five, so the average is over those three.
    A forward fill or an interpolated bar would move this number.
    """
    bars = pl.DataFrame([_bar(0, 10.0), _bar(1, 20.0), _bar(4, 30.0), _bar(5, 40.0)], schema=CANONICAL_SCHEMA)

    out = rolling_vwap(bars, window_minutes=5)

    # One row per bar that exists -- nothing emitted for 15:02 or 15:03.
    assert out.height == 4
    at_1504 = out.filter(pl.col("ts") == bars["ts"][2])["vwap"][0]
    assert at_1504 == pytest.approx((10.0 + 20.0 + 30.0) / 3)


def test_a_gap_longer_than_the_window_leaves_a_bar_alone_in_it():
    """After a break wider than the window, a bar's VWAP is its own typical price.

    Nothing from before the break is in range, which is what makes the first
    bar after a session break behave like the first bar of a series.
    """
    bars = pl.DataFrame(
        [_bar(0, 10.0), _bar(1, 20.0), _bar(61, 30.0), _bar(62, 40.0)], schema=CANONICAL_SCHEMA
    )

    out = rolling_vwap(bars, window_minutes=5)

    after_break = out.filter(pl.col("ts") == bars["ts"][2])["vwap"][0]
    assert after_break == pytest.approx(30.0)
    # The next bar sees only the two on its side of the break.
    assert out.filter(pl.col("ts") == bars["ts"][3])["vwap"][0] == pytest.approx(35.0)


def test_no_row_is_emitted_for_an_instant_inside_a_break():
    """A break yields no points of its own -- not a null, not a carried-forward value.

    Minutes 2 to 60 have no bar. 15:40 sits in the middle of that break, and its
    five-minute window (15:35, 15:40] would hold nothing, yet the output must not
    invent a row there to say so. Output timestamps are exactly the input's, so a
    consumer sees the break only as the jump from 15:01 to 16:01 -- which is why a
    chart has to mark it rather than rely on the data to.
    """
    bars = pl.DataFrame(
        [_bar(0, 10.0), _bar(1, 20.0), _bar(61, 30.0), _bar(62, 40.0)], schema=CANONICAL_SCHEMA
    )
    inside_break = bars["ts"][0] + dt.timedelta(minutes=40)

    out = rolling_vwap(bars, window_minutes=5)

    assert out["ts"].to_list() == bars["ts"].to_list()
    assert out.filter(pl.col("ts") == inside_break).is_empty()
    # No null placeholder anywhere: every row that exists has a real VWAP.
    assert out["vwap"].null_count() == 0
    # The last bar before the break keeps its own window; nothing leaks across.
    assert out["vwap"].to_list() == pytest.approx([10.0, 15.0, 30.0, 35.0])


def test_conflicting_duplicates_are_both_weighted_into_the_window(
    canonical_bars: pl.DataFrame,
):
    """Two bars on one instant are two observations, and both belong in the sum.

    Neither variant is more correct than the other -- that is what makes them a
    conflict -- so VWAP treats them as it treats any two bars in the window:
    each contributes its own typical price at its own volume. The instant then
    carries one VWAP, shared by both rows, while each row keeps the typical
    price it was built from.

    Checked against the same plain-Python oracle the contiguous case uses, so
    the expected numbers come from the definition of VWAP rather than from a
    second reading of the implementation.
    """
    conflict = canonical_bars.head(1).with_columns(
        pl.col("close") + 5.0, pl.lit("conflict").alias("row_hash")
    )
    bars = pl.concat([canonical_bars, conflict]).sort("ts")
    window = 15

    out = rolling_vwap(bars, window_minutes=window).sort("ts", "typical_price")

    rows = bars.sort("ts").to_dicts()
    expected = []
    for row in rows:
        lo = row["ts"] - dt.timedelta(minutes=window)
        in_window = [r for r in rows if lo < r["ts"] <= row["ts"]]
        pv = sum((r["high"] + r["low"] + r["close"]) / 3 * r["volume"] for r in in_window)
        vol = sum(r["volume"] for r in in_window)
        expected.append((row["ts"], (row["high"] + row["low"] + row["close"]) / 3, pv / vol))
    # Same ordering the output uses: the two rows on the clashing instant tie on
    # ts, so typical_price breaks the tie on both sides of the comparison.
    expected.sort(key=lambda e: (e[0], e[1]))

    # One row per input bar. A join on (contract, ts) to recover each bar's own
    # typical price used to fan out here -- 6 bars became 8 rows, half of them
    # pairing one variant's typical price with the other's VWAP.
    assert out.height == bars.height
    assert out["typical_price"].to_list() == pytest.approx([tp for _, tp, _ in expected])
    assert out["vwap"].to_list() == pytest.approx([v for _, _, v in expected])

    # The shared instant, stated directly: two typical prices, one VWAP, and it
    # is the volume-weighted mean of both variants rather than either alone.
    clash_ts = conflict["ts"][0]
    clashing = out.filter(pl.col("ts") == clash_ts)
    variants = bars.filter(pl.col("ts") == clash_ts).to_dicts()
    assert clashing.height == 2
    assert len(set(clashing["typical_price"].to_list())) == 2
    assert len(set(clashing["vwap"].to_list())) == 1

    tps = [(r["high"] + r["low"] + r["close"]) / 3 for r in variants]
    vols = [r["volume"] for r in variants]
    both = sum(tp * v for tp, v in zip(tps, vols, strict=True)) / sum(vols)
    assert clashing["vwap"][0] == pytest.approx(both)
    # ...and not the value either variant would have produced on its own.
    assert clashing["vwap"][0] != pytest.approx(tps[0])
    assert clashing["vwap"][0] != pytest.approx(tps[1])


@settings(max_examples=40, deadline=None)
@given(
    prices=st.lists(
        st.tuples(
            st.floats(1, 100),
            st.floats(1, 100),
            st.floats(1, 100),
            # Positive volume only. Negative volume survives ingestion, and a
            # window whose sum stays positive despite one can produce a VWAP
            # outside this range -- garbage in, and out of scope for a property
            # that describes what the maths does with sound input.
            st.integers(1, 1000),
        ),
        min_size=3,
        max_size=25,
    )
)
def test_vwap_within_window_price_range(prices):
    """Property test: a weighted average of prices can never leave their range.

    Hypothesis generates the bars, so this holds over shapes no hand-written
    fixture would cover. It is the invariant that catches a mis-weighted sum or
    a window joined to the wrong rows, both of which push the result outside
    the prices that produced it. The epsilon absorbs float rounding only.
    """
    base = dt.datetime(2024, 3, 4, 15, 0, tzinfo=dt.UTC)
    rows = []
    for i, (a, b, c, v) in enumerate(prices):
        lo, hi = min(a, b, c), max(a, b, c)
        rows.append(
            {
                "contract": "X",
                "root": "X",
                "exchange": "E",
                "frequency": "minute",
                "ts": base + dt.timedelta(minutes=i),
                "trading_date": base.date(),
                "open": (lo + hi) / 2,
                "high": hi,
                "low": lo,
                "close": (lo + hi) / 2,
                "volume": v,
                "open_interest": None,
                "source_file": "m",
                "row_hash": f"h{i}",
                "ingested_at": base,
            }
        )
    df = pl.DataFrame(rows, schema=CANONICAL_SCHEMA)
    out = rolling_vwap(df, window_minutes=5).drop_nulls("vwap")
    lo_all, hi_all = df["low"].min(), df["high"].max()
    assert out["vwap"].min() >= lo_all - 1e-6
    assert out["vwap"].max() <= hi_all + 1e-6
