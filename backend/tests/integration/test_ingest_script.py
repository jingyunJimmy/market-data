"""The `scripts/ingest.py` command line.

The script is the only way a user loads data, so its contract is the printed
summary and the exit code, not just what ends up in the database. These tests
drive `main()` directly with a temporary store and read back both.
"""

from __future__ import annotations

import importlib.util
import sys

import pytest

from market_data import config
from market_data.config import BACKEND_ROOT, FIXTURES_DIR
from market_data.services import AnalyticsService, QualityService
from market_data.storage import DuckDbRepository

pytestmark = pytest.mark.integration


def _load_script():
    """Import ``scripts/ingest.py``, which lives outside the installed package."""
    path = BACKEND_ROOT / "scripts" / "ingest.py"
    spec = importlib.util.spec_from_file_location("_ingest_script", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def run_ingest(tmp_path, monkeypatch):
    """`main` pointed at a throwaway database via the environment.

    The settings cache is cleared either side, otherwise the script would reuse
    whichever database path an earlier test had already resolved.
    """
    db_path = tmp_path / "ingest.duckdb"
    monkeypatch.setenv("MARKET_DATA_DB_PATH", str(db_path))
    config.get_settings.cache_clear()
    yield _load_script().main, db_path
    config.get_settings.cache_clear()


def test_ingest_reports_rejects_and_supersedes(run_ingest, capsys):
    """Bad rows are reported on stdout, and the run still succeeds.

    A dirty file is a normal outcome, not a failure: exit 0, with the counts
    visible. The store is then reopened to prove the summary matches what was
    actually written, including the conflict surfacing in the quality report.
    """
    main, db_path = run_ingest

    assert main([str(FIXTURES_DIR / "dirty_minute.csv")]) == 0
    out = capsys.readouterr().out
    assert "rejected" in out
    assert "superseded" in out

    repo = DuckDbRepository(db_path)
    repo.initialise()
    try:
        contracts = AnalyticsService(repo).contracts()
        assert any(c.contract == "CL_TEST" for c in contracts)
        report = QualityService(repo).report(contract="CL_TEST")
        assert any(i.code == "resolved_instant_conflict" for i in report.issues)
    finally:
        repo.close()


def test_ingest_accepts_a_directory(run_ingest, capsys):
    """A directory loads every file under it, and each is named in the summary.

    Two different formats in one run, so this also covers reader dispatch
    working through the command line rather than only in isolation.
    """
    main, _ = run_ingest

    assert main([str(FIXTURES_DIR)]) == 0
    out = capsys.readouterr().out
    assert "clean_minute.csv" in out
    assert "clean_daily.parquet" in out
    assert "Total:" in out


def test_explicit_frequency_overrides_inference(run_ingest, capsys):
    """The flag reaches normalisation, which is the only escape hatch a user has
    when a vendor file's name and columns do not identify it."""
    main, db_path = run_ingest

    assert main([str(FIXTURES_DIR / "clean_minute.csv"), "--frequency", "minute"]) == 0
    capsys.readouterr()

    repo = DuckDbRepository(db_path)
    repo.initialise()
    try:
        assert all(c.frequency == "minute" for c in AnalyticsService(repo).contracts())
    finally:
        repo.close()


def test_unmatched_path_exits_non_zero(run_ingest, capsys):
    """A path matching nothing is a user error, so it fails loudly on stderr.

    Path expansion returns an empty list rather than raising, so without this
    the run would exit 0 having silently ingested nothing at all.
    """
    main, _ = run_ingest

    assert main([str(FIXTURES_DIR / "nope.csv")]) == 1
    assert "No files matched" in capsys.readouterr().err
