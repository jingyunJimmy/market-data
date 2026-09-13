"""Format adapters: turn a file on disk into a raw Polars frame.

Readers are deliberately dumb -- they only understand *file formats*, not the
market-data schema. Every raw frame gets two bookkeeping columns:

* ``__source_file`` -- absolute path the row came from
* ``__row_number``  -- 1-based position within that file (for reject reporting)

Schema mapping, type coercion, and timestamp handling all happen later in
:mod:`market_data.ingestion.normalize`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

import polars as pl

SOURCE_COL = "__source_file"
ROW_NUM_COL = "__row_number"


class UnsupportedFileError(ValueError):
    """Raised when no reader recognises a file extension."""


@runtime_checkable
class FileReader(Protocol):
    """Reads one file into a raw frame. Implementations must be side-effect free."""

    extensions: tuple[str, ...]

    def read(self, path: Path) -> pl.DataFrame: ...


def _with_bookkeeping(df: pl.DataFrame, path: Path) -> pl.DataFrame:
    """Append the two provenance columns every raw frame carries.

    These let the later reject reporting point at an exact origin -- "file X,
    data row N" -- for any row that fails normalisation or quality checks.
    """
    return df.with_columns(
        # Constant column: the absolute source path, repeated on every row.
        pl.lit(str(path.resolve())).alias(SOURCE_COL),
        # 1-based position of the row within this file's data (header excluded).
        (pl.int_range(1, pl.len() + 1, dtype=pl.Int64)).alias(ROW_NUM_COL),
    )


class CsvReader:
    """CSV reader.

    Every field is read as text (``infer_schema_length=0``) so that malformed
    values survive to the normalisation step, where they are captured with a
    reason rather than silently coerced or dropped. Ragged lines are tolerated
    rather than fatal.
    """

    extensions: tuple[str, ...] = (".csv", ".txt")

    def read(self, path: Path) -> pl.DataFrame:
        df = pl.read_csv(
            path,
            # Disable type inference entirely: read every column as text so that
            # malformed values reach normalisation intact instead of being
            # silently coerced (or nulled) by the CSV parser.
            infer_schema_length=0,
            # Tolerate rows with too many/few fields by trimming extras rather
            # than aborting the whole file on the first ragged line.
            truncate_ragged_lines=True,
            # Treat these tokens as null on read, so downstream code only has to
            # check is_null() instead of matching every "missing" spelling.
            null_values=["", "NA", "N/A", "null", "NULL", "NaN"],
            # Never let the parser guess at date/datetime columns; timestamp
            # parsing is done explicitly (with known formats) in normalize.
            try_parse_dates=False,
        )
        return _with_bookkeeping(df, path)


class ParquetReader:
    extensions: tuple[str, ...] = (".parquet", ".pq")

    def read(self, path: Path) -> pl.DataFrame:
        return _with_bookkeeping(pl.read_parquet(path), path)


DEFAULT_READERS: tuple[FileReader, ...] = (CsvReader(), ParquetReader())


def reader_for(path: Path, readers: tuple[FileReader, ...] = DEFAULT_READERS) -> FileReader:
    """Picks the right reader for a single file, based on its extension."""
    suffix = path.suffix.lower()
    for reader in readers:
        if suffix in reader.extensions:
            return reader
    raise UnsupportedFileError(
        f"No reader for '{suffix}'. Supported: "
        + ", ".join(sorted({e for r in readers for e in r.extensions}))
    )


def expand_paths(inputs: list[Path]) -> list[Path]:
    """Resolve files, directories, and globs into a sorted list of files."""
    out: set[Path] = set()
    for raw in inputs:
        if any(ch in str(raw) for ch in "*?[") and not raw.exists():
            out.update(p for p in Path().glob(str(raw)) if p.is_file())
        elif raw.is_dir():
            out.update(p for p in raw.rglob("*") if p.is_file())
        elif raw.is_file():
            out.add(raw)
    return sorted(out)
