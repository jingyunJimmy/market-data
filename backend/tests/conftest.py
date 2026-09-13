"""Shared fixtures.

Two ways of getting data under test, used deliberately:

* the checked-in fixtures under `FIXTURES_DIR`, for anything that has to prove
  the real pipeline works end to end
* hand-built or synthetic frames, for anything that needs a specific defect the
  fixtures do not happen to contain

Every repository here is in-memory or under `tmp_path`, so tests never touch a
developer's real store and can run in any order.
"""

from __future__ import annotations

import datetime as dt

import polars as pl
import pytest

from market_data.config import FIXTURES_DIR
from market_data.domain.models import CANONICAL_SCHEMA
from market_data.ingestion import IngestionService
from market_data.storage import DuckDbRepository


@pytest.fixture
def repo() -> DuckDbRepository:
    """A fresh, initialised, in-memory store per test -- no cross-test bleed."""
    r = DuckDbRepository(":memory:")
    r.initialise()
    yield r
    r.close()


@pytest.fixture
def ingest(repo: DuckDbRepository) -> IngestionService:
    """Ingestion wired to that same store, so writes are visible through `repo`."""
    return IngestionService(repo)


@pytest.fixture
def loaded_repo(repo: DuckDbRepository, ingest: IngestionService) -> DuckDbRepository:
    """The store after the three shipped fixtures have been ingested.

    Clean minute, clean daily, and dirty minute. The clean and dirty minute
    files share one contract on purpose, so tests that need only clean bars
    filter on `source_file`.
    """
    ingest.ingest_file(FIXTURES_DIR / "clean_minute.csv")
    ingest.ingest_file(FIXTURES_DIR / "clean_daily.parquet")
    ingest.ingest_file(FIXTURES_DIR / "dirty_minute.csv")
    return repo


@pytest.fixture
def canonical_bars() -> pl.DataFrame:
    """A tiny hand-built canonical minute frame (5 bars, one contract).

    Already canonical, so it starts downstream of ingestion: analytics tests use
    it to assert on exact numbers without a normalisation step in between. The
    prices are chosen so the session's high (10.5) and low (9.6) each sit in the
    middle of the run rather than at an end.
    """
    base = dt.datetime(2024, 3, 4, 15, 0, tzinfo=dt.UTC)
    rows = []
    for i, (o, h, low, c, v) in enumerate(
        [
            (10.0, 10.5, 9.8, 10.2, 100),
            (10.2, 10.4, 10.0, 10.1, 120),
            (10.1, 10.3, 9.9, 10.0, 90),
            (10.0, 10.2, 9.7, 9.9, 110),
            (9.9, 10.1, 9.6, 9.8, 80),
        ]
    ):
        rows.append(
            {
                "contract": "CL_TEST",
                "root": "CL",
                "exchange": "NYMEX",
                "frequency": "minute",
                "ts": base + dt.timedelta(minutes=i),
                "trading_date": dt.date(2024, 3, 4),
                "open": o,
                "high": h,
                "low": low,
                "close": c,
                "volume": v,
                "open_interest": None,
                "source_file": "memory",
                "row_hash": f"h{i}",
                "ingested_at": base,
            }
        )
    return pl.DataFrame(rows, schema=CANONICAL_SCHEMA)


@pytest.fixture
def api_client(tmp_path, monkeypatch):
    """A FastAPI test client over an empty store in `tmp_path`.

    Both caches have to be cleared on the way in *and* out: settings and the
    repository are process-wide singletons, so a leftover one would point the
    next test at a previous test's database.
    """
    monkeypatch.setenv("MARKET_DATA_DB_PATH", str(tmp_path / "api.duckdb"))
    from market_data import config
    from market_data.api import deps

    config.get_settings.cache_clear()
    deps.reset_repo_cache()

    from fastapi.testclient import TestClient

    from market_data.api.main import create_app

    with TestClient(create_app()) as client:
        yield client

    config.get_settings.cache_clear()
    deps.reset_repo_cache()
