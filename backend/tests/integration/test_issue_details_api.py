"""Paging one finding's evidence past the sample the report carries.

The report ships at most `issue_detail_limit` details per issue so it stays a
reasonable size. These tests pin that the endpoint reaches beyond that sample,
that pages do not overlap or skip, and that the reported total is the real one.
"""

from __future__ import annotations

import polars as pl
import pytest

from market_data.api.deps import get_repo
from market_data.config import FIXTURES_DIR, get_settings
from market_data.domain.models import Frequency
from market_data.ingestion import IngestionService
from market_data.synthetic import clean_minute_bars

GAP_CODE = "intra_session_gap"


@pytest.fixture
def holed_api(api_client):
    """Minute data riddled with gaps, so a finding has more evidence than one page.

    5,000 bars minus every 7th yields ~713 single-bar gaps, comfortably past the
    500-row sample the report itself carries, which is what the deep-page test
    needs.
    """
    df = clean_minute_bars(minutes=5000)
    holed = df.with_row_index("i").filter(pl.col("i") % 7 != 0).drop("i")
    IngestionService(get_repo()).ingest_frame(holed, source="holed", frequency=Frequency.MINUTE)
    return api_client


@pytest.fixture
def dirty_api(api_client):
    """API client backed by the dirty fixture, which trips the invalid-value rules."""
    IngestionService(get_repo()).ingest_file(FIXTURES_DIR / "dirty_minute.csv")
    return api_client


def _details(client, code: str = GAP_CODE, **params):
    """One issue-details request for the fixture contract, asserted 200.

    Failing here rather than in the caller means a broken request shows its own
    response body instead of a KeyError further down.
    """
    r = client.get(
        "/quality/issue-details",
        params={"contract": "CL_TEST", "frequency": "minute", "code": code, **params},
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_issue_details_pages_without_overlap_or_skips(holed_api):
    """Two pages of evidence line up exactly with the head of the whole list."""
    whole = _details(holed_api, limit=1000)
    assert whole["total"] > 4

    first = _details(holed_api, limit=2, offset=0)
    second = _details(holed_api, limit=2, offset=2)

    assert [first["offset"], second["offset"]] == [0, 2]
    walked = [d["ts"] for d in first["details"] + second["details"]]
    assert walked == [d["ts"] for d in whole["details"][:4]]


def test_issue_details_reaches_past_the_reports_own_sample(holed_api):
    """The report caps details; the endpoint must not inherit that ceiling."""
    cap = get_settings().issue_detail_limit
    # Asserted, not skipped: a fixture that fell under the cap would leave this
    # test passing while exercising nothing.
    assert _details(holed_api, limit=1)["total"] > cap

    beyond = _details(holed_api, limit=2, offset=cap)
    assert beyond["offset"] == cap
    assert beyond["details"], "nothing returned past the sample cap"


def test_issue_details_total_survives_paging(holed_api):
    """The total is a property of the finding, not of the page you asked for.

    A total recomputed per page would shrink as the offset advanced, and the
    dashboard would report a different problem size on every scroll.
    """
    totals = {_details(holed_api, limit=1, offset=o)["total"] for o in (0, 1, 2)}
    assert len(totals) == 1


def test_issue_details_evidence_carries_the_bar_values(dirty_api):
    """Every check but the gap rules points at a real bar, so shows its OHLCV."""
    page = _details(dirty_api, code="high_lt_low", limit=10)

    assert page["details"]
    values = page["details"][0]["values"]
    assert {"open", "high", "low", "close", "volume"} <= set(values)
    assert values["high"] < values["low"]


def test_unknown_code_yields_an_empty_page_not_an_error(dirty_api):
    """A code that matches nothing is an empty result, not a 404.

    The dashboard requests details for whatever code it is showing, and a
    finding that has since cleared must not turn into an error page.
    """
    page = _details(dirty_api, code="no_such_check")

    assert page["total"] == 0
    assert page["details"] == []


def test_issue_details_rejects_a_negative_offset(api_client):
    """Same validation as the other paged endpoint, checked separately because
    it is a separate route rather than shared code."""
    r = api_client.get(
        "/quality/issue-details",
        params={"contract": "CL_TEST", "frequency": "minute", "code": "x", "offset": -1},
    )
    assert r.status_code == 422
