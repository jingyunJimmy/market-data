"""The HTTP surface.

These go through the real FastAPI app and a real store, so what is tested is
the wire contract: status codes, the JSON shape a client parses, and the
validation that turns a bad request into a 422 rather than a 500. The
computations themselves are covered by the unit tests, so assertions here stay
on what crossing the network boundary can break.
"""

from __future__ import annotations

import pytest

from market_data.config import FIXTURES_DIR

pytestmark = pytest.mark.integration


@pytest.fixture
def loaded_api(api_client):
    """API client backed by a store holding the clean + dirty minute fixtures."""
    from market_data.api.deps import get_repo
    from market_data.ingestion import IngestionService

    svc = IngestionService(get_repo())
    svc.ingest_file(FIXTURES_DIR / "clean_minute.csv")
    svc.ingest_file(FIXTURES_DIR / "dirty_minute.csv")
    return api_client


def test_health(api_client):
    """The endpoint a container probe hits, so it must answer without a store."""
    assert api_client.get("/health").json()["status"] == "ok"


def test_contracts_expose_available_date_range(loaded_api):
    """The listing the dashboard opens on: what exists, and over what span.

    The date range is what the UI seeds its pickers from, so an inverted or
    absent range would leave a user unable to select any data at all.
    """
    summaries = loaded_api.get("/contracts").json()
    minute = next(s for s in summaries if s["contract"] == "CL_TEST" and s["frequency"] == "minute")
    assert minute["bars"] > 0
    assert minute["first_date"] <= minute["last_date"]


def test_daily_ohlcv_aggregates_minute_bars(loaded_api):
    """Daily bars are derived on read from stored minute bars, not stored twice."""
    bars = loaded_api.get("/analytics/daily-ohlcv", params={"contract": "CL_TEST"}).json()
    assert bars and bars[0]["bar_count"] >= 1
    assert bars[0]["high"] >= bars[0]["low"]


def test_vwap_reports_window_and_truncation(loaded_api):
    """A capped series says so, rather than looking like a short one.

    Truncation is visible in the body (`total_points` against the returned
    points), so a client can tell "this is all of it" from "there is more".
    """
    series = loaded_api.get(
        "/analytics/vwap", params={"contract": "CL_TEST", "window_minutes": 15, "limit": 2}
    ).json()
    assert series["window_minutes"] == 15
    assert series["total_points"] > 2
    assert series["truncated"] is True
    assert len(series["points"]) == 2
    assert {"ts", "typical_price", "vwap"} == set(series["points"][0])


def test_quality_report_flags_dirty_data(loaded_api):
    """The findings reach the wire, including the one that has no bar behind it."""
    report = loaded_api.get("/quality/report", params={"contract": "CL_TEST", "frequency": "minute"}).json()
    assert report["bars_checked"] > 0
    codes = {i["code"] for i in report["issues"]}
    assert "high_lt_low" in codes
    # Instant conflicts are resolved at ingest, so they surface from the audit
    # trail rather than from `bars` -- resolved must not mean invisible.
    assert "resolved_instant_conflict" in codes


def test_contract_is_required(api_client):
    """A missing required parameter is a 422 from validation, not a 500 from a
    query that ran with no filter."""
    assert api_client.get("/analytics/vwap").status_code == 422


@pytest.fixture
def holed_api(api_client, tmp_path):
    """API client backed by minute data with a real intra-session hole punched in it.

    The checked-in fixtures are gap-free, so a listing test against them would
    pass while asserting nothing.
    """
    import datetime as dt

    import polars as pl

    from market_data.api.deps import get_repo
    from market_data.ingestion import IngestionService
    from market_data.synthetic import clean_minute_bars

    df = clean_minute_bars(minutes=180)
    holed = df.filter(
        ~pl.col("timestamp_chicago_wall").is_between(
            dt.datetime(2024, 3, 4, 10, 0), dt.datetime(2024, 3, 4, 10, 9)
        )
    )
    path = tmp_path / "holed_minute.csv"
    holed.write_csv(path)
    IngestionService(get_repo()).ingest_file(path)
    return api_client


def test_missing_timestamps_endpoint_lists_the_absent_instants(holed_api):
    """The listing over HTTP, against data with a hole punched in it.

    `len(timestamps) == total` because the limit is well above the count, so
    this also pins that the total is not something the endpoint invents.
    """
    r = holed_api.get(
        "/quality/missing-timestamps",
        params={"contract": "CL_TEST", "frequency": "minute", "limit": 1000},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["total"] > 0
    assert body["expected_interval_s"] == 60
    assert len(body["timestamps"]) == body["total"]
    # Expected breaks are excluded by design, so nothing here is a weekend.
    assert {t["classification"] for t in body["timestamps"]} <= {"intra_session", "extended"}


def test_missing_timestamps_endpoint_pages_without_overlap(holed_api):
    """Two consecutive pages equal the first four rows of the unpaged listing.

    Paging is applied server side, so the same walk that works in the unit test
    has to survive query-parameter parsing and serialisation.
    """

    def page(offset: int, limit: int = 2):
        """One page of the listing for the fixture contract."""
        return holed_api.get(
            "/quality/missing-timestamps",
            params={
                "contract": "CL_TEST",
                "frequency": "minute",
                "limit": limit,
                "offset": offset,
            },
        ).json()

    whole = page(0, 1000)
    assert whole["total"] > 4

    first, second = page(0), page(2)
    assert first["offset"] == 0 and second["offset"] == 2
    assert [t["ts"] for t in first["timestamps"] + second["timestamps"]] == [
        t["ts"] for t in whole["timestamps"][:4]
    ]


def test_missing_timestamps_rejects_a_negative_offset(api_client):
    """A negative offset is rejected by validation rather than silently clamped."""
    r = api_client.get(
        "/quality/missing-timestamps",
        params={"contract": "CL_TEST", "frequency": "minute", "offset": -1},
    )
    assert r.status_code == 422


def test_serve_entry_point_passes_its_flags_to_uvicorn(monkeypatch):
    """The ``market-data-serve`` console script declared in pyproject.toml."""
    import uvicorn

    from market_data.api.main import serve

    captured = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: captured.update(app=app, **kw))
    monkeypatch.setattr("sys.argv", ["market-data-serve", "--port", "9999", "--reload"])

    serve()

    # An import string, not the app object -- uvicorn needs that to reload.
    assert captured["app"] == "market_data.api.main:app"
    assert captured["port"] == 9999
    assert captured["reload"] is True
    assert captured["host"] == "127.0.0.1"
