"""Load CSV/Parquet market-data files into the local DuckDB store.

Usage::

    poetry run python scripts/ingest.py "data/raw/data/**/*.parquet"
    poetry run python scripts/ingest.py data/raw/data/minute/COMEX/GC
    poetry run python scripts/ingest.py somefile.csv --frequency minute

Accepts files, directories, or globs. Quote globs so the shell hands the
pattern through rather than expanding it itself.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from market_data.config import get_settings
from market_data.domain.models import Frequency
from market_data.ingestion import IngestionService
from market_data.storage import DuckDbRepository

EXAMPLES = """examples:
  poetry run python scripts/ingest.py "data/raw/data/**/*.parquet"
  poetry run python scripts/ingest.py data/raw/data/minute/COMEX/GC
  poetry run python scripts/ingest.py data/fixtures/dirty_minute.csv
  poetry run python scripts/ingest.py somefile.csv --frequency minute
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Load CSV/Parquet market-data files into the local DuckDB store.",
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("paths", nargs="+", type=Path, help="files, directories, or globs")
    parser.add_argument(
        "--frequency",
        type=Frequency,
        choices=list(Frequency),
        default=None,
        help="force frequency instead of inferring it from the path",
    )
    args = parser.parse_args(argv)

    repo = DuckDbRepository(get_settings().db_path)
    repo.initialise()
    try:
        results = IngestionService(repo).ingest_paths(args.paths, frequency=args.frequency)
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 1
    finally:
        repo.close()

    for r in results:
        line = f"{Path(r.source).name}: +{r.rows_ingested} bars, {r.rows_rejected} rejected"
        if r.rejections_by_reason:
            line += f" {r.rejections_by_reason}"
        # Kept separate from the reject tally: a superseded row was valid,
        # it just lost the one-bar-per-instant tie-break.
        if r.rows_superseded:
            line += f", {r.rows_superseded} superseded"
        print(line)

    total_in = sum(r.rows_ingested for r in results)
    total_rej = sum(r.rows_rejected for r in results)
    total_sup = sum(r.rows_superseded for r in results)
    summary = f"\nTotal: {total_in} ingested, {total_rej} rejected"
    if total_sup:
        summary += f", {total_sup} superseded (see superseded_bars)"
    print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
