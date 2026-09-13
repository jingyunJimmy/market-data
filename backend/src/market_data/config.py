"""Application configuration.

All settings are overridable via environment variables prefixed with
``MARKET_DATA_`` (e.g. ``MARKET_DATA_DB_PATH=/tmp/md.duckdb``).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

#: Backend root. This file is at ``backend/src/market_data/config.py``, so the
#: backend directory is three levels up. Data lives inside ``backend/`` so the
#: dataset and the DuckDB store sit next to the code that produces and reads them.
BACKEND_ROOT = Path(__file__).resolve().parents[2]

DATA_DIR = BACKEND_ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
"""Where ``scripts/fetch_data.py`` downloads the Hugging Face dataset."""
FIXTURES_DIR = DATA_DIR / "fixtures"
"""Small committed CSV/Parquet files the tests and the README quick-start use."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MARKET_DATA_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Storage
    db_path: Path = Field(default=DATA_DIR / "market_data.duckdb")

    # Analytics
    vwap_window_minutes: int = 15
    daily_bar_tz: str = "America/Chicago"
    source_minute_tz: str = "America/Chicago"

    # Quality thresholds
    outlier_mad_threshold: float = 8.0
    volume_spike_threshold: float = 20.0
    gap_min_multiple: float = 1.5
    """A gap is flagged when the spacing exceeds this multiple of the modal bar interval."""
    issue_detail_limit: int = 500
    """Per-issue cap on enumerated occurrences sent to the client.

    A quality issue can cover tens of thousands of bars -- a thin back-month
    contract produces ~9.5k intra-session gaps on its own -- and shipping every
    one would make the report heavier than the bars it describes. The client is
    told the real total and that the list was cut, so the figure stays honest.
    """

    # Intelligent insights, read by an LLM
    insights_max_occurrences: int = 50_000
    """Per-issue cap on occurrences the evidence statistics are computed from.

    Far above ``issue_detail_limit``: a distribution drawn from a 500-row
    sample of a 9,500-row finding would describe the sample, not the finding.
    The evidence records how many rows it actually analysed.
    """
    llm_model: str = "claude-sonnet-5"
    """The Claude model that reads the evidence."""
    llm_api_key: SecretStr | None = None
    """Defaults to the ``ANTHROPIC_API_KEY`` environment variable when unset."""
    llm_max_tokens: int = 16_000
    """Output cap per request."""
    llm_timeout_s: float = 180.0
    """Per request."""


@lru_cache
def get_settings() -> Settings:
    return Settings()
