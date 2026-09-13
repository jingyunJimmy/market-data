"""Download the evaluation dataset from Hugging Face into ``backend/data/raw/``.

Usage::

    poetry run python scripts/fetch_data.py            # curated subset (CL, ES, GC)
    poetry run python scripts/fetch_data.py --all      # all 80 parquet files
    poetry run python scripts/fetch_data.py --roots VX ZN

The dataset is public; no token is required.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from market_data.config import RAW_DIR

REPO_ID = "lynx1231/historical-futures-data-sample"
DEFAULT_ROOTS = ["CL", "ES", "GC"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all", action="store_true", help="fetch every contract")
    parser.add_argument("--roots", nargs="*", default=None, help="root symbols to fetch")
    parser.add_argument("--dest", type=Path, default=RAW_DIR, help="download directory")
    args = parser.parse_args()

    from huggingface_hub import snapshot_download

    if args.all:
        allow = None
    else:
        roots = args.roots or DEFAULT_ROOTS
        allow = ["*.md", "*.json", "catalog.*", "files.*", "schema.*"]
        allow += [f"data/*/*/{r}/*" for r in roots]

    path = snapshot_download(
        repo_id=REPO_ID,
        repo_type="dataset",
        local_dir=args.dest,
        allow_patterns=allow,
    )
    files = list(Path(path).rglob("*.parquet"))
    print(f"Downloaded {len(files)} parquet files to {path}")


if __name__ == "__main__":
    main()
