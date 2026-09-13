"""Unit tests for `normalize`, the raw-vendor-frame -> canonical-bar step.

Each test pins one decision the function makes. The four things worth
asserting on are the buckets a row can land in -- `clean`, `rejected`,
`superseded` -- and the counters that must make the arithmetic add up
(`rows_read`, `rows_deduplicated`, `rows_superseded`).
"""

from __future__ import annotations

import datetime as dt

import polars as pl
import pytest

from market_data.domain.models import CANONICAL_COLUMNS, Frequency
from market_data.ingestion.normalize import (
    REJECT_BAD_PRICE,
    REJECT_BAD_TIMESTAMP,
    REJECT_DST_LOCAL_TIME,
    REJECT_MISSING_COLUMNS,
    REJECT_NO_CONTRACT,
    REJECT_NO_OHLC,
    detect_frequency,
    normalize,
)


def _minute_raw(**over):
    """One well-formed minute row, in the shipped vendor's column names.

    Keyword arguments overwrite a field, so a test can introduce exactly one
    defect and attribute the outcome to it.
    """
    row = {
        "contract_symbol": "CLX24",
        "root": "CL",
        "exchange": "NYMEX",
        "timestamp_chicago_wall": dt.datetime(2024, 6, 3, 9, 0),
        "open": "78.0",
        "high": "78.2",
        "low": "77.9",
        "close": "78.1",
        "volume": "100",
    }
    row.update(over)
    return pl.DataFrame([row])


# --------------------------------------------------------------------------- #
# Frequency detection
#
# Frequency decides how the timestamp is read, so getting it wrong mis-stamps
# every bar in the file rather than failing loudly. Hence the priority chain:
# explicit override > path hint > column signature > minute.
# --------------------------------------------------------------------------- #


def test_detect_frequency_from_path():
    """The directory the file came from outranks anything in the frame."""
    df = _minute_raw()
    assert detect_frequency(df, path_hint="data/daily/NYMEX/CL/CLX24.parquet") is Frequency.DAILY
    assert detect_frequency(df, path_hint="data/minute/NYMEX/CL/CLX24.parquet") is Frequency.MINUTE


def test_detect_frequency_from_columns():
    """With no path to go on, a marker column identifies the file: only daily
    bars carry open interest."""
    daily = pl.DataFrame([{"contract_symbol": "X", "open_interest": 1, "date": "2024-01-01"}])
    assert detect_frequency(daily) is Frequency.DAILY


# --------------------------------------------------------------------------- #
# Timestamp semantics
#
# The dataset splits them by frequency, and this is the trap the whole module
# exists for: minute files carry a tz-naive Chicago wall clock, daily files
# carry a real UTC-midnight epoch. One is converted, the other is stamped.
# --------------------------------------------------------------------------- #


def test_minute_timestamp_is_localised_to_utc():
    """A wall clock is localised to Chicago, then converted -- never stamped UTC.

    The offset is read from the tz database for that date, not hard-coded: the
    same 09:00 maps to 15:00 UTC in January (CST) and 14:00 in June (CDT).
    """
    res = normalize(_minute_raw(), frequency=Frequency.MINUTE)
    assert res.clean.height == 1
    # 09:00 Chicago (CDT, UTC-5) in June -> 14:00 UTC
    assert res.clean["ts"][0] == dt.datetime(2024, 6, 3, 14, 0, tzinfo=dt.UTC)
    assert set(CANONICAL_COLUMNS) == set(res.clean.columns)


def test_daily_timestamp_from_epoch_ms():
    """The daily epoch is already a UTC instant, so it is stamped, not shifted.

    Also pins `trading_date`, which for daily bars is just that UTC date.
    """
    raw = pl.DataFrame(
        [
            {
                "contract_symbol": "CLX24",
                "timestamp_ms": 1_704_067_200_000,  # 2024-01-01 00:00 UTC
                "open": 70.0,
                "high": 71.0,
                "low": 69.0,
                "close": 70.5,
                "volume": 10,
                "open_interest": 5,
            }
        ]
    )
    res = normalize(raw, frequency=Frequency.DAILY)
    assert res.clean["ts"][0] == dt.datetime(2024, 1, 1, tzinfo=dt.UTC)
    assert res.clean["trading_date"][0] == dt.date(2024, 1, 1)


def test_dst_spring_forward_is_rejected():
    """A wall clock inside the spring-forward gap gets its own reject reason.

    It must not collapse into `unparseable_timestamp`: the value parsed fine,
    it just names an instant that never happened, which points at the vendor's
    export rather than at a corrupt field.
    """
    # 2024-03-10 02:30 America/Chicago does not exist (clocks jump 02:00 -> 03:00)
    res = normalize(
        _minute_raw(timestamp_chicago_wall=dt.datetime(2024, 3, 10, 2, 30)),
        frequency=Frequency.MINUTE,
    )
    assert res.clean.is_empty()
    assert res.rejected["reason"].to_list() == [REJECT_DST_LOCAL_TIME]


# --------------------------------------------------------------------------- #
# What gets rejected, and what only gets flagged
#
# The line is structural: a row that cannot form a bar is rejected here, a row
# that forms an implausible bar is kept and left to the quality layer. Moving
# that line is what these tests are here to catch.
# --------------------------------------------------------------------------- #


def test_rejects_missing_contract_and_bad_price():
    """Two structurally broken rows, each reported under its own reason.

    Both defects in one frame, so a single catch-all reason would fail here.
    """
    raw = pl.DataFrame(
        [
            {**_minute_raw().row(0, named=True), "contract_symbol": None},
            {**_minute_raw().row(0, named=True), "close": "not-a-number"},
        ],
        schema=_minute_raw().schema,
    )
    res = normalize(raw, frequency=Frequency.MINUTE)
    reasons = set(res.rejected["reason"].to_list())
    assert reasons == {REJECT_NO_CONTRACT, REJECT_BAD_PRICE}
    assert res.clean.is_empty()


@pytest.mark.parametrize("bad", [{"high": "77.0", "low": "78.0"}])
def test_suspicious_but_usable_rows_are_kept(bad):
    """An impossible bar is still a bar: it survives ingestion and is reported
    by the quality layer, where the user can see it and decide."""
    # high < low is a *quality* problem, not an ingestion reject
    res = normalize(_minute_raw(**bad), frequency=Frequency.MINUTE)
    assert res.clean.height == 1


# --------------------------------------------------------------------------- #
# One bar per instant
#
# The read side assumes it, so the conflict is settled at ingestion. The two
# ways a row can disappear are deliberately kept apart: dedup loses nothing,
# superseding loses a value someone may later want to explain.
# --------------------------------------------------------------------------- #


def test_exact_duplicates_collapsed_with_count():
    """The same file listing a bar twice yields one row, and the count says so."""
    res = normalize(pl.concat([_minute_raw(), _minute_raw()], how="vertical"), frequency=Frequency.MINUTE)
    assert res.clean.height == 1
    assert res.rows_deduplicated == 1


def test_conflicting_instants_keep_the_last_row_in_file_order():
    """Same (contract, ts), different values -> latest wins, loser is returned."""
    rows = [
        {
            "contract_symbol": "X",
            "timestamp_chicago_wall": "2024-03-04 09:00:00",
            "open": 1.0,
            "high": 2.0,
            "low": 0.5,
            "close": 1.5,
            "volume": 10,
        },
        # same instant, later in the file, different close -> this one wins
        {
            "contract_symbol": "X",
            "timestamp_chicago_wall": "2024-03-04 09:00:00",
            "open": 1.0,
            "high": 2.0,
            "low": 0.5,
            "close": 1.9,
            "volume": 20,
        },
        {
            "contract_symbol": "X",
            "timestamp_chicago_wall": "2024-03-04 09:01:00",
            "open": 1.9,
            "high": 2.1,
            "low": 1.8,
            "close": 2.0,
            "volume": 30,
        },
    ]
    result = normalize(pl.DataFrame(rows), frequency=Frequency.MINUTE)

    assert result.clean.height == 2
    assert result.rows_superseded == 1
    assert result.clean.filter(pl.col("volume") == 20).height == 1  # the later row survived
    assert result.clean.filter(pl.col("volume") == 10).is_empty()

    # The displaced row is kept whole and points at what replaced it, so the
    # tie-break can be audited against the stored bar.
    loser = result.superseded.row(0, named=True)
    assert loser["close"] == 1.5
    assert loser["reason"] == "superseded_in_file"
    assert loser["winning_row_hash"] in result.clean["row_hash"].to_list()


def test_byte_identical_rows_are_deduplicated_not_superseded():
    """An exact duplicate loses nothing, so it must not enter the audit trail."""
    row = {
        "contract_symbol": "X",
        "timestamp_chicago_wall": "2024-03-04 09:00:00",
        "open": 1.0,
        "high": 2.0,
        "low": 0.5,
        "close": 1.5,
        "volume": 10,
    }
    result = normalize(pl.DataFrame([row, dict(row)]), frequency=Frequency.MINUTE)

    assert result.clean.height == 1
    assert result.rows_deduplicated == 1
    assert result.rows_superseded == 0
    assert result.superseded.is_empty()


# --------------------------------------------------------------------------- #
# Vendor file shapes
#
# The dataset we ship fixtures for is one vendor's layout. These cover the
# fallbacks normalize keeps for files that name the same things differently,
# and the whole-file bail-out for input that is not bar data at all.
# --------------------------------------------------------------------------- #


def test_frequency_defaults_to_minute_when_nothing_identifies_the_file():
    """No path hint, no marker column: guessing daily would silently mis-stamp."""
    bare = pl.DataFrame([{"symbol": "X", "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5}])
    assert detect_frequency(bare) is Frequency.MINUTE


def test_vendor_price_and_symbol_aliases_map_to_canonical_columns():
    """A file that names every column differently still normalises.

    `settle -> close` is the one that matters: a daily settlement price is the
    close, and dropping it would leave the bar with no close at all.
    """
    raw = pl.DataFrame(
        [
            {
                "ticker": "CLX24",
                "timestamp_chicago_wall": "2024-06-03 09:00:00",
                "px_open": 78.0,
                "px_high": 78.2,
                "px_low": 77.9,
                "settle": 78.1,
                "qty": 100,
            }
        ]
    )
    res = normalize(raw, frequency=Frequency.MINUTE)

    assert res.clean.height == 1
    row = res.clean.row(0, named=True)
    assert row["contract"] == "CLX24"
    assert row["close"] == 78.1  # settle -> close
    assert row["volume"] == 100  # qty -> volume


def test_minute_falls_back_to_a_plain_timestamp_column():
    """A file without the Chicago wall column is still localised, not stamped UTC."""
    raw = _minute_raw()
    raw = raw.drop("timestamp_chicago_wall").with_columns(pl.lit("2024-06-03 09:00:00").alias("timestamp"))
    res = normalize(raw, frequency=Frequency.MINUTE)

    assert res.clean.height == 1
    assert res.clean["ts"][0] == dt.datetime(2024, 6, 3, 14, 0, tzinfo=dt.UTC)


def test_minute_falls_back_to_a_datetime_column():
    """Same fallback, one step further down the chain of accepted column names."""
    raw = _minute_raw()
    raw = raw.drop("timestamp_chicago_wall").with_columns(pl.lit("2024-06-03 09:00:00").alias("datetime"))
    res = normalize(raw, frequency=Frequency.MINUTE)

    assert res.clean["ts"][0] == dt.datetime(2024, 6, 3, 14, 0, tzinfo=dt.UTC)


def test_daily_falls_back_to_a_plain_timestamp_column_stamped_utc():
    """Daily labels are already UTC midnight, so they are stamped, never shifted."""
    raw = _minute_raw()
    raw = raw.drop("timestamp_chicago_wall").with_columns(pl.lit("2024-06-03 00:00:00").alias("timestamp"))
    res = normalize(raw, frequency=Frequency.DAILY)

    assert res.clean["ts"][0] == dt.datetime(2024, 6, 3, 0, 0, tzinfo=dt.UTC)


def test_rows_with_no_usable_timestamp_column_are_rejected_not_dropped():
    """The row count must survive: a rejected row is reported, never lost."""
    raw = _minute_raw().drop("timestamp_chicago_wall")
    res = normalize(raw, frequency=Frequency.MINUTE)

    assert res.clean.is_empty()
    assert res.rows_read == 1
    assert res.rejected["reason"].to_list() == [REJECT_BAD_TIMESTAMP]


def test_a_file_that_is_not_bar_data_rejects_every_row_with_one_reason():
    """No OHLC and no timestamp: limping on would invent bars out of nothing."""
    raw = pl.DataFrame({"note": ["header junk", "more junk"], "n": [1, 2]})
    res = normalize(raw, frequency=Frequency.MINUTE)

    assert res.clean.is_empty()
    assert res.rejected.height == 2
    assert res.rejections_by_reason == {REJECT_MISSING_COLUMNS: 2}
    # Same shape as every other reject report. It used to keep the vendor's own
    # columns, which made persisting the rejections fail on the file that needed
    # reporting most.
    assert res.rejected.columns == ["source_file", "row_number", "reason"]


def test_a_partial_bar_is_rejected_rather_than_crashing_on_the_absent_column():
    """A close-only file has a timestamp, so it survives Step 1 and must be
    classified per row -- the missing OHLC columns are materialised as null
    first so the row-level expressions have something to look at."""
    raw = _minute_raw().drop("open", "high", "low")
    res = normalize(raw, frequency=Frequency.MINUTE)

    assert res.clean.is_empty()
    assert res.rejected["reason"].to_list() == [REJECT_NO_OHLC]


def test_a_file_with_no_symbol_column_at_all_is_rejected_for_the_right_reason():
    """`contract` absent entirely must read as "no contract", not as a crash."""
    raw = _minute_raw().drop("contract_symbol")
    res = normalize(raw, frequency=Frequency.MINUTE)

    assert res.clean.is_empty()
    assert res.rejected["reason"].to_list() == [REJECT_NO_CONTRACT]
