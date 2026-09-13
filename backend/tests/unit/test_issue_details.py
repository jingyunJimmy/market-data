"""Per-occurrence evidence attached to quality issues.

The summary ("N intra-session gaps") tells a user something is wrong; the
details say where. These tests pin the three things the dashboard relies on:
the evidence points at the right instants, the cap is applied, and the reported
total stays the real one even when the list was cut.
"""

from __future__ import annotations

import datetime as dt

import polars as pl

from market_data.config import Settings
from market_data.domain.models import Frequency, IssueDetail
from market_data.domain.quality import QualityValidator
from market_data.domain.quality.details import date_detail, detail_fields
from market_data.ingestion.normalize import normalize
from market_data.synthetic import clean_daily_bars, clean_minute_bars, dirty_minute_bars


def _issue(report, code):
    """The one finding under test, or a StopIteration that says it never fired."""
    return next(i for i in report.issues if i.code == code)


# --------------------------------------------------------------------------- #
# detail_fields
# --------------------------------------------------------------------------- #


def test_detail_fields_reports_complete_list_as_untruncated():
    """Nothing cut, nothing flagged: the baseline the truncating cases differ from."""
    rows = [IssueDetail(label=str(i)) for i in range(3)]
    fields = detail_fields(rows, limit=10)
    assert fields["detail_total"] == 3
    assert fields["detail_truncated"] is False
    assert len(fields["details"]) == 3


def test_detail_fields_caps_and_flags_truncation():
    """Over the cap, the list is trimmed but the count still tells the truth.

    A trimmed list with a trimmed total would silently understate the problem,
    which is exactly what the truncation flag exists to prevent.
    """
    rows = [IssueDetail(label=str(i)) for i in range(10)]
    fields = detail_fields(rows, limit=4)
    assert len(fields["details"]) == 4
    assert fields["detail_total"] == 10
    assert fields["detail_truncated"] is True
    # Truncation keeps the *first* rows, so callers must pass them in reading order.
    assert [d.label for d in fields["details"]] == ["0", "1", "2", "3"]


def test_detail_fields_keeps_the_real_total_when_caller_pre_sliced():
    """The path the rules take: slice the frame, then declare the true count."""
    rows = [IssueDetail(label=str(i)) for i in range(4)]
    fields = detail_fields(rows, limit=4, total=9594)
    assert fields["detail_total"] == 9594
    assert fields["detail_truncated"] is True


def test_detail_fields_total_never_undercounts_what_it_ships():
    """A wrong `total` must not make the list look longer than it is."""
    rows = [IssueDetail(label=str(i)) for i in range(5)]
    fields = detail_fields(rows, limit=10, total=2)
    assert fields["detail_total"] == 5
    assert fields["detail_truncated"] is False


def test_date_detail_labels_by_date_and_stamps_utc_midnight():
    """A date-shaped finding still needs an instant the chart can place.

    The label is what a user reads, the UTC-midnight timestamp is what the
    dashboard positions the marker with, so both are pinned here.
    """
    detail = date_detail(dt.date(2025, 12, 25))
    assert detail.label == "2025-12-25"
    assert detail.ts == dt.datetime(2025, 12, 25, tzinfo=dt.UTC)


# --------------------------------------------------------------------------- #
# daily: one row per absent session
# --------------------------------------------------------------------------- #


def test_daily_gap_details_name_every_missing_session():
    """Every absent session is enumerated, not just counted.

    `detail_total == count` is the assertion that matters: the evidence and the
    headline number must be describing the same set of findings.
    """
    df = clean_daily_bars(days=60)
    dropped = sorted(df["date"].to_list())[10:13]
    holed = df.filter(~pl.col("date").is_in(dropped))

    res = normalize(holed, frequency=Frequency.DAILY)
    report = QualityValidator().validate(res.clean, frequency=Frequency.DAILY)
    issue = _issue(report, "missing_sessions")

    listed = {d.label for d in issue.details}
    assert {str(d) for d in dropped} <= listed
    assert issue.detail_total == issue.count
    assert issue.detail_truncated is False
    # A session is a point, not a span.
    assert all(d.end_ts is None for d in issue.details)


# --------------------------------------------------------------------------- #
# minute: one row per gap, spanning the bars that should have been there
# --------------------------------------------------------------------------- #


def test_minute_gap_detail_spans_the_absent_bars():
    """A minute gap is one row covering a run, not one row per absent bar.

    Thousands of single-bar rows would be unusable, so a run collapses into a
    span. The arithmetic below is what keeps the span honest.
    """
    df = clean_minute_bars(minutes=180)
    lo = dt.datetime(2024, 3, 4, 10, 0)
    hi = dt.datetime(2024, 3, 4, 10, 30)
    holed = df.filter(~pl.col("timestamp_chicago_wall").is_between(lo, hi))

    res = normalize(holed, frequency=Frequency.MINUTE)
    report = QualityValidator().validate(res.clean, frequency=Frequency.MINUTE)
    issue = _issue(report, "intra_session_gap")

    assert issue.details, "the gap should be enumerated, not just counted"
    gap = max(issue.details, key=lambda d: d.values["missing_bars"])
    # The run starts one bar after the last present bar and ends one before the
    # next, so the span is the absent bars themselves -- not the bars either side.
    assert gap.ts is not None and gap.end_ts is not None
    assert gap.ts < gap.end_ts
    spanned = int((gap.end_ts - gap.ts).total_seconds() / 60) + 1
    assert spanned == gap.values["missing_bars"]


def test_minute_gap_details_respect_the_configured_cap():
    """The cap limits the evidence shipped, never what the report claims to have found."""
    df = clean_minute_bars(minutes=400)
    # Punch out every 10th bar so the frame is riddled with single-bar gaps.
    holed = df.with_row_index("i").filter(pl.col("i") % 10 != 0).drop("i")

    res = normalize(holed, frequency=Frequency.MINUTE)
    validator = QualityValidator(settings=Settings(issue_detail_limit=5))
    report = validator.validate(res.clean, frequency=Frequency.MINUTE)
    issue = _issue(report, "intra_session_gap")

    assert len(issue.details) == 5
    assert issue.detail_total > 5
    assert issue.detail_truncated is True
    # The cap must not distort the headline count.
    assert issue.detail_total == issue.count


def test_rules_without_evidence_stay_at_the_defaults():
    """Checks not yet wired for details must still produce valid issues."""
    df = clean_minute_bars(minutes=180)
    res = normalize(df, frequency=Frequency.MINUTE)
    report = QualityValidator().validate(res.clean, frequency=Frequency.MINUTE)
    for issue in report.issues:
        if issue.category != "gap":
            assert issue.details == []
            assert issue.detail_total == 0
            assert issue.detail_truncated is False


# --------------------------------------------------------------------------- #
# invalid values, outliers, duplicates and conflicts
# --------------------------------------------------------------------------- #


def _dirty_report():
    """Validate the dirty fixture, returning the report and the normalise result.

    The result comes back too because the superseded rows live there, not in
    `clean`, and one test below needs both halves.
    """
    res = normalize(dirty_minute_bars(), frequency=Frequency.MINUTE)
    return QualityValidator().validate(res.clean, frequency=Frequency.MINUTE), res


def test_invalid_value_evidence_carries_the_offending_ohlcv():
    """The diagnosis *is* the numbers: "high < low" means nothing without them."""
    report, _ = _dirty_report()
    issue = _issue(report, "high_lt_low")

    assert issue.details
    values = issue.details[0].values
    assert {"open", "high", "low", "close", "volume"} <= set(values)
    # The row really does violate the predicate it was flagged for.
    assert values["high"] < values["low"]


def test_negative_volume_evidence_shows_the_negative_volume():
    """Evidence must show the offending value, not a neighbouring row's."""
    report, _ = _dirty_report()
    issue = _issue(report, "negative_volume")
    assert all(d.values["volume"] < 0 for d in issue.details)


def test_price_outlier_evidence_carries_the_return_and_its_score():
    """A flagged bar shows both the move and how far outside normal it sat.

    The score is what makes the finding arguable: the threshold is a setting,
    so a user needs to see the value it was compared against.
    """
    report, _ = _dirty_report()
    issue = _issue(report, "price_return_outlier")

    assert issue.details
    for detail in issue.details:
        assert {"log_return", "mad_score"} <= set(detail.values)
        # Every listed bar cleared the configured threshold.
        assert detail.values["mad_score"] > Settings().outlier_mad_threshold


def test_volume_spike_evidence_carries_the_baseline_it_was_judged_against():
    """A spike is only a spike relative to something, so the baseline travels with it.

    Without the local median a user cannot tell a genuine surge from a quiet
    period that made an ordinary bar look large.
    """
    df = clean_minute_bars(minutes=200)
    spiked = df.with_columns(
        pl.when(pl.int_range(pl.len()) == 120)
        .then(pl.col("volume") * 500)
        .otherwise(pl.col("volume"))
        .alias("volume")
    )
    res = normalize(spiked, frequency=Frequency.MINUTE)
    report = QualityValidator().validate(res.clean, frequency=Frequency.MINUTE)
    issue = _issue(report, "volume_spike")

    assert issue.details
    detail = issue.details[0]
    assert {"volume", "local_median", "x_median"} <= set(detail.values)
    assert detail.values["volume"] > detail.values["local_median"]


def test_resolved_conflict_evidence_shows_what_the_discarded_row_said():
    """The only drill-down surfacing data no other view in the app can reach."""
    res = normalize(dirty_minute_bars(), frequency=Frequency.MINUTE)
    assert res.superseded.height, "fixture must actually contain a displaced row"

    report = QualityValidator().validate(res.clean, frequency=Frequency.MINUTE, superseded=res.superseded)
    issue = _issue(report, "resolved_instant_conflict")

    assert len(issue.details) == res.superseded.height
    values = issue.details[0].values
    assert {"open", "high", "low", "close", "volume", "reason"} <= set(values)
    assert values["reason"] == "superseded_in_file"
    # The instant still holds a bar -- the winner -- so these values survive
    # nowhere in `clean`. Without this drill-down they would be unreachable.
    assert issue.details[0].ts in set(res.clean["ts"].to_list())
    survivor = res.clean.filter(pl.col("ts") == issue.details[0].ts).row(0, named=True)
    assert (survivor["open"], survivor["close"]) != (values["open"], values["close"])


def test_every_wired_rule_reports_a_total_matching_its_evidence():
    """detail_total must never disagree with what the rule actually found."""
    _, res = _dirty_report()
    report = QualityValidator().validate(res.clean, frequency=Frequency.MINUTE, superseded=res.superseded)
    wired = [i for i in report.issues if i.details]
    assert wired, "the fixture should trip several wired rules"
    for issue in wired:
        assert issue.detail_total >= len(issue.details)
        assert issue.detail_truncated == (issue.detail_total > len(issue.details))


# --------------------------------------------------------------------------- #
# raw OHLC on every evidence row
# --------------------------------------------------------------------------- #


def test_outlier_evidence_carries_the_bar_that_produced_it():
    """A return of 0.11 could be a bad print or a real move; only prices say which."""
    report, _ = _dirty_report()
    for code in ("price_return_outlier",):
        issue = _issue(report, code)
        for detail in issue.details:
            assert {"open", "high", "low", "close", "volume"} <= set(detail.values)


def test_duplicate_evidence_carries_the_duplicated_bar_values():
    """The duplicate's own values, matched back against the frame it came from.

    The final lookup is what rules out placeholder or default values being
    passed off as the bar's real ones.
    """
    res = normalize(dirty_minute_bars(), frequency=Frequency.MINUTE)
    doubled = pl.concat([res.clean, res.clean.head(2)])
    report = QualityValidator().validate(doubled, frequency=Frequency.MINUTE)

    exact = _issue(report, "exact_duplicate")
    values = exact.details[0].values
    assert {"row_hash", "copies"} <= set(values)
    assert {"open", "high", "low", "close", "volume"} <= set(values)

    # The attached values are the bar's real ones, not placeholders.
    match = doubled.filter(pl.col("row_hash") == values["row_hash"]).row(0, named=True)
    assert (match["open"], match["close"]) == (values["open"], values["close"])
