"""Persistence layer: the :class:`BarRepository` port and its DuckDB adapter."""

from market_data.storage.duckdb_repo import DuckDbRepository
from market_data.storage.repository import BarRepository

__all__ = ["BarRepository", "DuckDbRepository"]
