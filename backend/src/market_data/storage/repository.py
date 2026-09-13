"""The persistence port.

Application services depend on this Protocol, never on DuckDB directly, so the
store can be swapped (a warehouse, Postgres, an in-memory fake for tests)
without touching business logic.
"""

from __future__ import annotations

import datetime as dt
from typing import Protocol

import polars as pl

from market_data.domain.models import ContractSummary, Frequency, IngestionResult


class BarRepository(Protocol):
    def initialise(self) -> None: ...

    def insert_bars(self, bars: pl.DataFrame, *, run_id: str | None = None) -> int:
        """Upsert canonical bars; return the number of new instants stored.

        One bar per ``(contract, frequency, ts)``: a later write wins, and the
        row it displaced is preserved for audit. Re-ingesting unchanged data
        therefore stores nothing new and returns 0.
        """

    def record_run(self, result: IngestionResult) -> None:
        """Append an ingestion run to the audit trail."""

    def record_rejections(self, run_id: str, rejected: pl.DataFrame) -> None:
        """Append the rows a run could not normalise, with their reasons."""

    def record_superseded(self, run_id: str, superseded: pl.DataFrame) -> None:
        """Append the valid rows a run displaced under the last-write-wins rule."""

    def contracts(self) -> list[ContractSummary]:
        """Catalogue of everything ingested, one row per (contract, frequency)."""

    def load_bars(
        self,
        *,
        contract: str | None = None,
        frequency: Frequency = Frequency.MINUTE,
        start: dt.date | None = None,
        end: dt.date | None = None,
    ) -> pl.DataFrame: ...

    def load_superseded(
        self,
        *,
        contract: str | None = None,
        frequency: Frequency = Frequency.MINUTE,
        start: dt.date | None = None,
        end: dt.date | None = None,
    ) -> pl.DataFrame:
        """Displaced bars matching the same filter, for quality reporting."""
