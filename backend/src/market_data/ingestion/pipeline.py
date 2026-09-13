"""The ingestion orchestrator: read -> normalise -> persist -> report."""

from __future__ import annotations

import datetime as dt
import uuid
from pathlib import Path

import polars as pl

from market_data.config import Settings, get_settings
from market_data.domain.models import Frequency, IngestionResult
from market_data.ingestion.normalize import NormalizeResult, normalize
from market_data.ingestion.readers import DEFAULT_READERS, FileReader, expand_paths, reader_for
from market_data.storage.repository import BarRepository


class IngestionService:
    def __init__(
        self,
        repo: BarRepository,
        *,
        settings: Settings | None = None,
        readers: tuple[FileReader, ...] = DEFAULT_READERS,
    ) -> None:
        self._repo = repo
        self._settings = settings or get_settings()
        self._readers = readers

    # -- public API ------------------------------------------------------ #

    def ingest_paths(
        self, inputs: list[Path], *, frequency: Frequency | None = None
    ) -> list[IngestionResult]:
        files = expand_paths([Path(p) for p in inputs])
        if not files:
            raise FileNotFoundError(f"No files matched: {inputs}")
        return [self.ingest_file(f, frequency=frequency) for f in files]

    def ingest_file(self, path: Path, *, frequency: Frequency | None = None) -> IngestionResult:
        path = Path(path)
        raw = reader_for(path, self._readers).read(path)
        return self._run(raw, source=str(path.resolve()), path_hint=str(path), frequency=frequency)

    def ingest_frame(
        self,
        raw: pl.DataFrame,
        *,
        source: str,
        frequency: Frequency | None = None,
        path_hint: str = "",
    ) -> IngestionResult:
        return self._run(raw, source=source, path_hint=path_hint or source, frequency=frequency)

    # -- internals ----------------------------------------------------- #

    def _run(
        self,
        raw: pl.DataFrame,
        *,
        source: str,
        path_hint: str,
        frequency: Frequency | None,
    ) -> IngestionResult:
        started = dt.datetime.now(dt.UTC)
        run_id = uuid.uuid4().hex

        result: NormalizeResult = normalize(
            raw,
            frequency=frequency,
            path_hint=path_hint,
            source_tz=self._settings.source_minute_tz,
            daily_tz=self._settings.daily_bar_tz,
            ingested_at=started,
        )

        inserted = self._repo.insert_bars(result.clean, run_id=run_id)
        contracts = sorted(result.clean["contract"].unique().to_list()) if result.clean.height else []

        ingestion = IngestionResult(
            run_id=run_id,
            source=source,
            started_at=started,
            finished_at=dt.datetime.now(dt.UTC),
            rows_read=result.rows_read,
            rows_ingested=inserted,
            rows_rejected=result.rejected.height,
            rows_deduplicated=result.rows_deduplicated,
            rows_superseded=result.rows_superseded,
            rejections_by_reason=result.rejections_by_reason,
            contracts=contracts,
        )
        self._repo.record_rejections(run_id, result.rejected)
        self._repo.record_superseded(run_id, result.superseded)
        self._repo.record_run(ingestion)
        return ingestion
