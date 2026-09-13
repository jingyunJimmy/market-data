"""The quality checks, one rule at a time.

Rules are called directly with a hand-built `RuleContext` rather than through
the validator, so a failure names the rule that broke. What is asserted is the
issue *code* and its *severity*: severity is what the dashboard sorts and
colours by, and several rules here fire at a different level depending on
frequency, which is the part most easily got wrong.
"""

from __future__ import annotations

import datetime as dt

import polars as pl

from market_data.domain.models import CANONICAL_SCHEMA, Frequency, Severity
from market_data.domain.quality import QualityValidator
from market_data.domain.quality.checks.duplicates import conflicting_duplicates
from market_data.domain.quality.checks.outliers import price_return_outliers
from market_data.domain.quality.checks.values import invalid_values
from market_data.domain.quality.rules import RuleContext


def _bars(rows: list[dict]) -> pl.DataFrame:
    """Canonical bars from partial rows: give the fields a test cares about.

    Everything else is filled with a valid default and timestamps advance a
    minute per row, so each test states only the defect it is about.
    """
    base = dt.datetime(2024, 3, 4, 15, 0, tzinfo=dt.UTC)
    filled = []
    for i, r in enumerate(rows):
        filled.append(
            {
                "contract": "X",
                "root": "X",
                "exchange": "E",
                "frequency": r.get("frequency", "minute"),
                "ts": r.get("ts", base + dt.timedelta(minutes=i)),
                "trading_date": dt.date(2024, 3, 4),
                "open": 10.0,
                "high": 10.5,
                "low": 9.5,
                "close": 10.0,
                "volume": 100,
                "open_interest": None,
                "source_file": "m",
                "row_hash": r.get("row_hash", f"h{i}"),
                "ingested_at": base,
                **{k: v for k, v in r.items() if k in CANONICAL_SCHEMA},
            }
        )
    return pl.DataFrame(filled, schema=CANONICAL_SCHEMA)


# --------------------------------------------------------------------------- #
# Single-row predicates
#
# A bar judged on its own fields. The interesting part is not that the defect is
# found but at what severity: the same predicate means different things at
# different frequencies, because a daily bar carries exchange conventions a
# minute bar does not.
# --------------------------------------------------------------------------- #


def test_high_lt_low_is_error():
    """The one invariant no convention can excuse: a bar whose high is below its
    low is impossible at any frequency, so it is always an error."""
    ctx = RuleContext(bars=_bars([{"high": 1.0, "low": 9.0}]), frequency=Frequency.MINUTE)
    issues = list(invalid_values(ctx))
    assert any(i.code == "high_lt_low" and i.severity is Severity.ERROR for i in issues)


def test_daily_close_out_of_range_is_only_info():
    """A daily settlement is set by the exchange and legitimately lands outside
    the day's traded range, so this is informational rather than a defect."""
    bars = _bars([{"close": 99.0, "frequency": "daily"}])
    ctx = RuleContext(bars=bars, frequency=Frequency.DAILY)
    issue = next(i for i in invalid_values(ctx) if i.code == "close_out_of_range")
    assert issue.severity is Severity.INFO


def test_minute_close_out_of_range_is_error():
    """The same predicate on a minute bar is a real error: nothing settles
    intraday, so a close outside [low, high] is a broken bar.

    This test and the one above are a pair. Together they pin that severity is
    read from the frequency, and neither is meaningful without the other.
    """
    ctx = RuleContext(bars=_bars([{"close": 99.0}]), frequency=Frequency.MINUTE)
    issue = next(i for i in invalid_values(ctx) if i.code == "close_out_of_range")
    assert issue.severity is Severity.ERROR


def test_negative_volume_flagged():
    """Ingestion keeps a negative volume because the bar is still usable, so
    this layer is the only thing standing between it and the user."""
    ctx = RuleContext(bars=_bars([{"volume": -5}]), frequency=Frequency.MINUTE)
    assert any(i.code == "negative_volume" for i in invalid_values(ctx))


# --------------------------------------------------------------------------- #
# Bars sharing an instant
#
# Two rows on one timestamp can mean two different things, and the severity has
# to follow which one it is.
# --------------------------------------------------------------------------- #


def test_conflicting_duplicates_detected():
    """Two different prices for one instant: one of them is wrong, and nothing
    in the data says which. An error, because a human has to decide."""
    ts = dt.datetime(2024, 3, 4, 15, 0, tzinfo=dt.UTC)
    bars = _bars(
        [
            {"ts": ts, "close": 10.0, "row_hash": "a"},
            {"ts": ts, "close": 12.0, "row_hash": "b"},
        ]
    )
    issues = list(conflicting_duplicates(RuleContext(bars=bars, frequency=Frequency.MINUTE)))
    assert any(i.code == "conflicting_duplicate" and i.severity is Severity.ERROR for i in issues)


def test_repeated_identical_timestamp_is_only_warning():
    """The same bar twice agrees with itself, so nothing is in doubt.

    Worth reporting as a sign of a sloppy export, but not an error. The exact
    list assertion is the point: it must not also raise a conflict.
    """
    ts = dt.datetime(2024, 3, 4, 15, 0, tzinfo=dt.UTC)
    bars = _bars([{"ts": ts, "row_hash": "a"}, {"ts": ts, "row_hash": "b"}])
    issues = list(conflicting_duplicates(RuleContext(bars=bars, frequency=Frequency.MINUTE)))
    assert [i.code for i in issues] == ["repeated_timestamp"]
    assert issues[0].severity is Severity.WARNING


# --------------------------------------------------------------------------- #
# Through the validator, against stored data
#
# The two tests that use the real store rather than a hand-built frame, so the
# calendar logic meets data that actually went through ingestion.
# --------------------------------------------------------------------------- #


def test_daily_missing_sessions(loaded_repo):
    """Dropping one business day mid-series must be noticed as a missing session."""
    bars = loaded_repo.load_bars(contract="CL_TEST", frequency=Frequency.DAILY)
    # drop a mid-series business day
    victim = bars["trading_date"][10]
    holey = bars.filter(pl.col("trading_date") != victim)
    report = QualityValidator().validate(holey, contract="CL_TEST", frequency=Frequency.DAILY)
    assert any(i.code == "missing_sessions" for i in report.issues)


def test_clean_minute_fixture_has_no_errors(loaded_repo):
    """The negative case, and the one that catches over-eager rules.

    Every other test here proves a rule fires. This proves the whole set stays
    quiet on data with nothing wrong with it, which is what makes a finding
    elsewhere worth trusting.
    """
    bars = loaded_repo.load_bars(contract="CL_TEST", frequency=Frequency.MINUTE)
    # the dirty fixture shares the contract; restrict to the clean day/rows via hash-free check
    clean = bars.filter(pl.col("source_file").str.contains("clean_minute"))
    report = QualityValidator().validate(clean, contract="CL_TEST", frequency=Frequency.MINUTE)
    assert report.errors() == []


# --------------------------------------------------------------------------- #
# Statistical guards
#
# The outlier check is median/MAD based, so it has two ways to be meaningless:
# too few observations to describe a distribution, and a distribution with no
# spread. Both must stay silent rather than flag everything.
# --------------------------------------------------------------------------- #


def test_outlier_check_stays_silent_below_its_minimum_sample():
    """Under 30 bars, median/MAD describes nothing, so it must not accuse."""
    short = _bars([{"close": 10.0 + i} for i in range(29)])  # wild moves, tiny sample
    assert list(price_return_outliers(RuleContext(bars=short, frequency=Frequency.MINUTE))) == []


def test_outlier_check_stays_silent_when_the_returns_have_no_spread():
    """A locked market gives MAD = 0; dividing by it would flag every move."""
    flat = _bars([{"close": 10.0} for _ in range(59)] + [{"close": 12.0}])
    assert list(price_return_outliers(RuleContext(bars=flat, frequency=Frequency.MINUTE))) == []


def test_outlier_check_does_flag_a_jump_once_it_has_a_baseline():
    """The mirror of the two guards above: with a real sample it must accuse."""
    walk = [{"close": 10.0 + (i % 3) * 0.01} for i in range(60)]
    walk[45] = {"close": 30.0}
    issues = list(price_return_outliers(RuleContext(bars=_bars(walk), frequency=Frequency.MINUTE)))
    assert any(i.code == "price_return_outlier" for i in issues)


def test_rules_over_an_empty_frame_iterate_zero_contracts():
    """The grouping every rule is built on yields nothing on an empty frame, so
    no rule needs its own empty-input branch."""
    ctx = RuleContext(bars=pl.DataFrame(schema=CANONICAL_SCHEMA), frequency=Frequency.MINUTE)
    assert list(ctx.per_contract()) == []
