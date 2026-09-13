"""The synthetic generators, and the round trip back through the pipeline.

The generators are what the fixtures, the demo data and half the tests are
built from, so a silent change in what they emit would weaken every test that
depends on them without failing anything. These tests pin their contract:
"clean" really is clean, and "dirty" really does carry the defects the quality
tests expect to find.
"""

from __future__ import annotations

import datetime as dt

import polars as pl

from market_data.domain.models import CANONICAL_SCHEMA, Frequency
from market_data.domain.quality import QualityValidator
from market_data.ingestion.normalize import normalize
from market_data.synthetic import clean_daily_bars, clean_minute_bars, dirty_minute_bars


def test_clean_minute_bars_are_valid_ohlc():
    """The OHLC invariants a real bar obeys, so "clean" means something.

    Tests elsewhere assert that clean data raises no errors. If the generator
    ever emitted a high below its low, those tests would fail somewhere far
    from the cause.
    """
    df = clean_minute_bars(minutes=200)
    assert df.height == 200
    assert (df["high"] >= df["low"]).all()
    assert (df["high"] >= df["close"]).all()
    assert (df["low"] <= df["open"]).all()


def test_clean_daily_bars_business_days_only():
    """No weekend sessions: the gap rules treat a weekend as an expected break,
    so a generator that emitted Saturdays would quietly mask that logic."""
    df = clean_daily_bars(days=30)
    weekdays = df["date"].dt.weekday()
    assert weekdays.max() <= 5


def test_dirty_minute_bars_round_trip_through_normalize():
    """The dirty generator carries exactly the defects downstream tests expect.

    Two collapses from the injected duplicate copies, and no reject reason
    outside the three it deliberately plants. The subset assertion is the
    important half: a new, unintended reason appearing here would mean the
    fixture had started testing something nobody wrote a test for.
    """
    res = normalize(dirty_minute_bars(), frequency=Frequency.MINUTE)
    assert res.rows_deduplicated == 2  # base row0 + two injected copies -> 2 collapse
    assert set(res.rejections_by_reason) <= {
        "unparseable_timestamp",
        "unparseable_price",
        "missing_ohlc",
    }


def test_minute_gap_classification():
    """Generator -> normalize -> validator, the full read path in one test.

    A hole punched mid-session has to come out the far end as an intra-session
    gap, which is what proves the three layers agree on the time axis.
    """
    df = clean_minute_bars(minutes=180)
    # remove a 30-minute block mid-session -> intra-session gap
    hole = df.filter(
        ~pl.col("timestamp_chicago_wall").is_between(
            dt.datetime(2024, 3, 4, 10, 0), dt.datetime(2024, 3, 4, 10, 30)
        )
    )
    res = normalize(hole, frequency=Frequency.MINUTE)
    report = QualityValidator().validate(res.clean, contract="CL_TEST", frequency=Frequency.MINUTE)
    assert any(i.code == "intra_session_gap" for i in report.issues)


def test_validator_on_empty_frame_is_clean():
    """No bars means no findings. A rule that mistakes "nothing" for "nothing
    good" would light up the dashboard for a contract that simply has no data
    in the selected range."""
    report = QualityValidator().validate(pl.DataFrame(schema=CANONICAL_SCHEMA), frequency=Frequency.MINUTE)
    assert report.issues == []
    assert report.bars_checked == 0
