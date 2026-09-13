"""Insights step 1: the statistics every provider reasons from.

A pattern is only as right as the distribution it was read from, so these pin
the dimensions that decide one: which clock the hours are counted on, what a
share is out of, and how "after a gap" is measured. Reports are hand-built so
each test states exactly the occurrences it is about.
"""

from __future__ import annotations

import datetime as dt

from market_data.config import Settings
from market_data.domain.insights.evidence import build_evidence_pack
from market_data.domain.models import (
    Frequency,
    IssueCategory,
    IssueDetail,
    QualityIssue,
    QualityReport,
    Severity,
)
from market_data.domain.quality.details import date_detail

# June 2024 is daylight time in Chicago: 16:00 CT is 21:00 UTC.
MONDAY = dt.date(2024, 6, 3)


def _utc(day: dt.date, hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime.combine(day, dt.time(hour, minute), tzinfo=dt.UTC)


def _halt(day: dt.date) -> IssueDetail:
    """The daily 16:00-17:00 CT halt as a gap detail: 60 missing bars, 61 minutes between bars."""
    return IssueDetail(
        ts=_utc(day, 21), end_ts=_utc(day, 21, 59), values={"missing_bars": 60, "gap_minutes": 61.0}
    )


def _issue(code: str, details: list[IssueDetail], **over: object) -> QualityIssue:
    fields: dict[str, object] = {
        "category": IssueCategory.GAP,
        "code": code,
        "severity": Severity.WARNING,
        "contract": "ESZ25",
        "frequency": Frequency.MINUTE,
        "message": code,
        "count": len(details),
        "details": details,
        "detail_total": len(details),
        **over,
    }
    return QualityIssue.model_validate(fields)


def _pack(*issues: QualityIssue, frequency: Frequency = Frequency.MINUTE, trading_days: int = 5, **settings):
    report = QualityReport(
        contract="ESZ25", frequency=frequency, start=None, end=None, bars_checked=5000, issues=list(issues)
    )
    return build_evidence_pack(report, trading_days=trading_days, settings=Settings(**settings))


def _week() -> list[dt.date]:
    return [MONDAY + dt.timedelta(days=i) for i in range(5)]


def test_hours_are_counted_in_exchange_time_not_utc():
    """21:00 UTC is the 16:00 CT halt. Counted in UTC it would look like nothing in particular."""
    pack = _pack(
        _issue("intra_session_gap", [_halt(d) for d in _week()], context={"expected_interval_s": 60})
    )

    (evidence,) = pack.evidence
    assert evidence.top_hours[0].hour == 16
    assert evidence.top_hours[0].share == 1.0
    assert evidence.days_affected == 5
    assert evidence.days_affected_share == 1.0
    assert evidence.weekday_counts == {"Mon": 1, "Tue": 1, "Wed": 1, "Thu": 1, "Fri": 1}
    assert evidence.median_duration_min == 61.0


def test_a_late_evening_occurrence_belongs_to_the_chicago_date():
    """03:00 UTC Tuesday is 22:00 CT Monday, the session the rest of the app files it under."""
    late = IssueDetail(ts=_utc(MONDAY + dt.timedelta(days=1), 3))
    pack = _pack(_issue("volume_spike", [late], category=IssueCategory.OUTLIER))

    assert pack.evidence[0].weekday_counts == {"Mon": 1}
    assert pack.evidence[0].top_hours[0].hour == 22


def test_a_daily_finding_keeps_its_session_date_and_has_no_hours():
    """A daily ts is a date at UTC midnight. Shifted into Chicago it would land on the previous day."""
    missing = _issue("missing_sessions", [date_detail(MONDAY), date_detail(MONDAY + dt.timedelta(days=2))])
    pack = _pack(missing, frequency=Frequency.DAILY, trading_days=250)

    (evidence,) = pack.evidence
    assert evidence.weekday_counts == {"Mon": 1, "Wed": 1}
    assert evidence.top_hours == []
    # A missing session is not one of the sessions checked, so it has no share of them.
    assert evidence.days_affected_share is None


def test_share_after_gap_counts_occurrences_close_behind_a_resumed_series():
    """The halt's last missing bar is 16:59 CT, so bars resume at 17:00 and 17:02 is within five minutes."""
    gaps = _issue("intra_session_gap", [_halt(MONDAY)], context={"expected_interval_s": 60})
    outliers = _issue(
        "price_return_outlier",
        [IssueDetail(ts=_utc(MONDAY, 22, 2)), IssueDetail(ts=_utc(MONDAY, 18))],
        category=IssueCategory.OUTLIER,
    )
    pack = _pack(gaps, outliers)

    assert pack.by_id()["E2"].share_after_gap == 0.5
    assert pack.by_id()["E1"].share_after_gap is None  # a gap is not "after a gap"


def test_the_interval_is_inferred_for_gap_findings_that_do_not_state_it():
    """Only intra_session_gap states its interval; a session break's own detail implies it."""
    breaks = _issue(
        "session_break",
        [
            IssueDetail(
                ts=_utc(MONDAY, 22),
                end_ts=_utc(MONDAY, 22, 58),
                values={"missing_bars": 59, "gap_minutes": 60.0},
            )
        ],
        severity=Severity.INFO,
    )
    assert _pack(breaks).evidence[0].expected_interval_s == 60


def test_analysed_says_how_many_occurrences_the_statistics_came_from():
    """A capped list must not pass for the whole finding."""
    capped = _issue(
        "intra_session_gap", [_halt(MONDAY)] * 3, count=900, detail_total=900, detail_truncated=True
    )

    (evidence,) = _pack(capped).evidence
    assert (evidence.occurrences, evidence.analysed) == (900, 3)
    assert evidence.per_1k_bars == 180.0  # from the real count, not the sample


def test_a_finding_without_occurrences_carries_its_counts_only():
    """Nothing to distribute, so no distribution -- rather than one computed from nothing."""
    (evidence,) = _pack(_issue("exact_duplicate", [], count=2, category=IssueCategory.DUPLICATE)).evidence

    assert evidence.analysed == 0
    assert evidence.top_hours == [] and evidence.days_affected is None


def test_the_trend_bucket_widens_with_the_span():
    """Weekly over two years is a hundred numbers nobody reads."""

    def spanning(years: int):
        later = IssueDetail(ts=_utc(MONDAY.replace(year=MONDAY.year + years), 15))
        return _pack(_issue("volume_spike", [IssueDetail(ts=_utc(MONDAY, 15)), later])).evidence[0]

    week = _pack(_issue("volume_spike", [IssueDetail(ts=_utc(d, 15)) for d in _week()])).evidence[0]

    assert week.period == "week"
    assert spanning(2).period == "month"
    assert spanning(5).period == "quarter"
    assert [p.count for p in spanning(5).period_counts] == [1, 1]  # empty buckets are not listed


def test_the_thresholds_in_force_travel_with_the_pack():
    """A threshold suggestion is only checkable against the value it proposes to replace."""
    pack = _pack(outlier_mad_threshold=9.0)

    assert pack.thresholds == {
        "outlier_mad_threshold": 9.0,
        "volume_spike_threshold": 20.0,
        "gap_min_multiple": 1.5,
    }
    assert pack.scope.timezone == "America/Chicago"
