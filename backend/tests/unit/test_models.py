"""Derived fields on the response models.

These are small, but they are what the dashboard paginates and summarises on,
so a wrong `has_more` strands the last page and a wrong severity tally
understates the problem the report exists to show.
"""

from __future__ import annotations

import datetime as dt

import pytest

from market_data.domain.models import (
    Frequency,
    IssueCategory,
    IssueDetail,
    IssueDetailPage,
    MissingTimestamp,
    MissingTimestampPage,
    QualityIssue,
    QualityReport,
    Severity,
)


def _issue(severity: Severity, *, count: int = 1, code: str = "c") -> QualityIssue:
    """One issue with only the fields the tallies read varied; the rest fixed."""
    return QualityIssue(
        category=IssueCategory.INVALID_VALUE,
        code=code,
        severity=severity,
        contract="X",
        frequency=Frequency.MINUTE,
        message="m",
        count=count,
    )


def _page(*, total: int, offset: int, n: int) -> IssueDetailPage:
    """A page holding `n` rows at `offset`, out of `total` overall."""
    return IssueDetailPage(
        contract="X",
        frequency=Frequency.MINUTE,
        code="c",
        total=total,
        offset=offset,
        details=[IssueDetail(label=str(i)) for i in range(n)],
    )


@pytest.mark.parametrize(
    ("total", "offset", "n", "expected"),
    [
        (100, 0, 20, True),  # first page of many
        (100, 80, 20, False),  # exactly consumed
        (100, 90, 20, False),  # a short final page
        (0, 0, 0, False),  # nothing to page through
    ],
)
def test_has_more_is_driven_by_offset_plus_page_size(total, offset, n, expected):
    """`has_more` counts what the caller has actually seen, not the page size asked for.

    The two boundary cases are the ones that strand a client in a loop: a final
    page that lands exactly on `total`, and a short one that stops before it.
    Both must report False even though the request looked like every other page.
    """
    assert _page(total=total, offset=offset, n=n).has_more is expected


def test_missing_timestamp_page_pages_on_the_same_rule():
    """A second page type must not grow a second paging rule.

    Same shape, same arithmetic: one row at offset 2 of 3 is the end.
    """
    page = MissingTimestampPage(
        contract="X",
        frequency=Frequency.MINUTE,
        total=3,
        offset=2,
        timestamps=[
            MissingTimestamp(
                contract="X",
                ts=dt.datetime(2024, 3, 4, tzinfo=dt.UTC),
                classification="intra_session",
            )
        ],
    )
    assert page.has_more is False


def test_severity_tally_counts_occurrences_not_issues():
    """One issue can stand for 9594 gaps; a tally of issues would read as 1."""
    report = QualityReport(
        contract="X",
        frequency=Frequency.MINUTE,
        start=None,
        end=None,
        bars_checked=10,
        issues=[
            _issue(Severity.ERROR, count=3, code="a"),
            _issue(Severity.ERROR, count=2, code="b"),
            _issue(Severity.WARNING, count=7, code="c"),
        ],
    )

    # Two error issues summing to 5 occurrences, so the tally cannot be a
    # per-issue count and cannot be lumping severities together either.
    assert report.by_severity() == {"error": 5, "warning": 7}
    assert [i.code for i in report.errors()] == ["a", "b"]


def test_a_clean_report_tallies_nothing():
    """No issues means empty, not a dict of zeros: the dashboard renders on
    presence, so a zero entry would draw a severity band with nothing in it."""
    report = QualityReport(contract="X", frequency=Frequency.MINUTE, start=None, end=None, bars_checked=10)
    assert report.by_severity() == {}
    assert report.errors() == []
