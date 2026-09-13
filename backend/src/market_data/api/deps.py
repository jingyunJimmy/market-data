"""Dependency wiring for the API (one DuckDB-backed repository + its services)."""

from __future__ import annotations

from functools import lru_cache

from market_data.config import get_settings
from market_data.services import AnalyticsService, QualityService
from market_data.storage import DuckDbRepository


@lru_cache
def _repo() -> DuckDbRepository:
    repo = DuckDbRepository(get_settings().db_path)
    repo.initialise()
    return repo


def get_repo() -> DuckDbRepository:
    return _repo()


def get_analytics_service() -> AnalyticsService:
    return AnalyticsService(get_repo())


def get_quality_service() -> QualityService:
    return QualityService(get_repo())


def reset_repo_cache() -> None:
    """Test helper -- drop the cached repo so a new DB path takes effect."""
    _repo.cache_clear()
