"""Dependency wiring for the API (one DuckDB-backed repository + its services)."""

from __future__ import annotations

from functools import lru_cache

from market_data.config import get_settings
from market_data.llm import ClaudeClient
from market_data.services import AnalyticsService, InsightsService, QualityService
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


def get_insights_service() -> InsightsService:
    settings = get_settings()
    key = settings.llm_api_key.get_secret_value() if settings.llm_api_key else None
    client = ClaudeClient(
        model=settings.llm_model,
        api_key=key,
        timeout_s=settings.llm_timeout_s,
        max_tokens=settings.llm_max_tokens,
    )
    return InsightsService(get_repo(), client=client)


def reset_repo_cache() -> None:
    """Test helper -- drop the cached repo so a new DB path takes effect."""
    _repo.cache_clear()
