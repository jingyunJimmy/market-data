"""Ingestion layer: read raw files, normalise to the canonical schema, persist."""

from market_data.ingestion.pipeline import IngestionService
from market_data.ingestion.readers import CsvReader, FileReader, ParquetReader, reader_for

__all__ = ["CsvReader", "FileReader", "IngestionService", "ParquetReader", "reader_for"]
