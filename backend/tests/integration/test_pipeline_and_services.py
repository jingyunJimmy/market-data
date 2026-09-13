"""Ingestion through storage to the service layer.

Where the unit tests each pin one function, these run the real path -- a file
on disk, normalisation, DuckDB, then the services the API calls -- and assert
on what survives it. The recurring theme is the one-bar-per-instant rule:
re-ingesting, correcting, and conflicting rows must all leave exactly one bar
per instant with the displaced values still recoverable.
"""

from __future__ import annotations

import polars as pl
import pytest

from market_data.config import FIXTURES_DIR
from market_data.domain.analytics import daily_ohlcv
from market_data.domain.models import Frequency
from market_data.services import AnalyticsService, QualityService

pytestmark = pytest.mark.integration


def test_ingestion_persists_and_is_idempotent(repo, ingest):
    """Loading the same file twice leaves the store exactly as it was.

    Re-ingesting is a normal operation, so the second run must add nothing and
    displace nothing: identical values overwriting an instant are not a
    conflict, and the audit trail stays empty.
    """
    first = ingest.ingest_file(FIXTURES_DIR / "clean_daily.parquet")
    assert first.rows_ingested > 0
    again = ingest.ingest_file(FIXTURES_DIR / "clean_daily.parquet")
    assert again.rows_ingested == 0  # upsert overwrote each instant with itself
    assert again.rows_superseded == 0  # identical values are not a conflict
    assert repo.load_bars(frequency=Frequency.DAILY).height == first.rows_ingested
    assert repo.load_superseded(frequency=Frequency.DAILY).is_empty()


def test_dirty_file_reports_rejections(ingest):
    """The dirty fixture's defects are counted and attributed, not just tolerated.

    An exact set rather than a subset, so a new rejection reason appearing here
    fails instead of being absorbed.
    """
    result = ingest.ingest_file(FIXTURES_DIR / "dirty_minute.csv")
    assert result.rows_rejected == 3
    assert set(result.rejections_by_reason) == {
        "unparseable_timestamp",
        "unparseable_price",
        "missing_ohlc",
    }
    assert result.rows_deduplicated == 2


def test_analytics_service_filters_by_contract(loaded_repo):
    """The contract filter reaches the query, so one contract's view never
    includes another's bars."""
    svc = AnalyticsService(loaded_repo)
    assert "CL_TEST" in {c.contract for c in svc.contracts()}
    daily = svc.daily_bars(contract="CL_TEST")
    assert daily and {b.contract for b in daily} == {"CL_TEST"}


def test_vwap_series_caps_points_and_flags_truncation(loaded_repo):
    """The limit is applied and declared, so a client can tell a capped series
    from a short one."""
    series = AnalyticsService(loaded_repo).vwap(contract="CL_TEST", window_minutes=15, limit=3)
    assert series.window_minutes == 15
    assert series.truncated and len(series.points) == 3
    assert series.total_points > 3


def test_computed_daily_matches_the_minute_bars_it_came_from(repo, ingest):
    """The derived daily bar agrees with the minute bars underneath it.

    Checked against the source data rather than a hard-coded number, so it
    stays honest if the fixture is regenerated.
    """
    ingest.ingest_file(FIXTURES_DIR / "clean_minute.csv")
    minute = repo.load_bars(contract="CL_TEST", frequency=Frequency.MINUTE)
    daily = daily_ohlcv(minute)
    # single synthetic session
    assert daily.height == 1
    row = daily.row(0, named=True)
    assert row["high"] == minute["high"].max()
    assert row["low"] == minute["low"].min()
    assert row["volume"] == minute["volume"].sum()


def test_later_run_corrects_an_earlier_bar(repo, ingest, tmp_path):
    """A vendor correction overwrites the instant and files the old row away.

    The same bar arriving twice across two runs, which is how a real correction
    turns up. Its reason differs from an in-file conflict -- `superseded_by_run`
    against `superseded_in_file` -- so the two causes stay distinguishable.
    """
    header = "contract_symbol,root,exchange,timestamp_chicago_wall,trading_date,open,high,low,close,volume"
    row = "CL_FIX,CL,NYMEX,2024-03-04 09:00:00,2024-03-04,78.0,78.1,77.9,{close},{volume}"
    original = tmp_path / "v1.csv"
    original.write_text(f"{header}\n{row.format(close=78.05, volume=100)}\n")
    correction = tmp_path / "v2.csv"
    correction.write_text(f"{header}\n{row.format(close=78.25, volume=250)}\n")

    ingest.ingest_file(original)
    second = ingest.ingest_file(correction)

    # Overwriting an existing instant is not a new bar.
    assert second.rows_ingested == 0
    bars = repo.load_bars(contract="CL_FIX", frequency=Frequency.MINUTE)
    assert bars.height == 1
    assert bars["close"][0] == pytest.approx(78.25)
    assert bars["volume"][0] == 250

    displaced = repo.load_superseded(contract="CL_FIX", frequency=Frequency.MINUTE)
    assert displaced.height == 1
    assert displaced["reason"][0] == "superseded_by_run"
    assert displaced["close"][0] == pytest.approx(78.05)


def test_resolved_conflicts_stay_visible_in_the_quality_report(repo, ingest):
    """A conflict resolved at ingest is still reported, and re-ingesting does
    not inflate it.

    The finding counts conflicting *instants*, which is a property of the source
    data, while the audit rows accumulate per run. Conflating the two would make
    the problem appear to grow every time the same file was loaded.
    """
    ingest.ingest_file(FIXTURES_DIR / "dirty_minute.csv")
    report = QualityService(repo).report(contract="CL_TEST", frequency=Frequency.MINUTE)

    issue = next(i for i in report.issues if i.code == "resolved_instant_conflict")
    assert issue.count == 4

    # Re-ingesting appends fresh audit rows but must not inflate the finding:
    # 4 instants conflicted in the source however many times we load it.
    ingest.ingest_file(FIXTURES_DIR / "dirty_minute.csv")
    again = QualityService(repo).report(contract="CL_TEST", frequency=Frequency.MINUTE)
    reissued = next(i for i in again.issues if i.code == "resolved_instant_conflict")
    assert reissued.count == 4
    assert reissued.context["discarded_rows"] == 8


def test_a_file_that_is_not_bar_data_is_reported_not_raised(repo, ingest, tmp_path):
    """Pointing the ingester at the wrong file must produce a rejection report.

    Regression: the whole-file bail-out used to hand back the vendor's own
    columns instead of the reject schema, so persisting the rejections raised
    ColumnNotFoundError -- the one file whose rejections matter most.
    """
    junk = tmp_path / "notes.csv"
    junk.write_text("note,n\nheader junk,1\nmore junk,2\n")

    result = ingest.ingest_file(junk)

    assert result.rows_read == 2
    assert result.rows_ingested == 0
    assert result.rows_rejected == 2
    assert result.rejections_by_reason == {"missing_required_columns": 2}
    assert repo.load_bars(frequency=Frequency.MINUTE).is_empty()


def test_a_frame_can_be_ingested_without_touching_the_disk(repo, ingest):
    """`ingest_frame` is the seam tests and notebooks use instead of a file."""
    raw = pl.DataFrame(
        [
            {
                "contract_symbol": "CLZ24",
                "timestamp_chicago_wall": "2024-06-03 09:00:00",
                "open": 78.0,
                "high": 78.2,
                "low": 77.9,
                "close": 78.1,
                "volume": 100,
            }
        ]
    )

    result = ingest.ingest_frame(raw, source="notebook", frequency=Frequency.MINUTE)

    assert result.rows_ingested == 1
    assert result.source == "notebook"  # the run record carries the caller's label
    stored = repo.load_bars(frequency=Frequency.MINUTE)
    # The bar's own provenance comes from the reader's bookkeeping column, which
    # an in-memory frame has none of, so it falls back rather than inventing a path.
    assert stored["source_file"].to_list() == ["<memory>"]
