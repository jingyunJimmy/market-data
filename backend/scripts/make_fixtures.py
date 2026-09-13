"""(Re)generate the committed test fixtures in ``backend/data/fixtures/``."""

from __future__ import annotations

from market_data.config import FIXTURES_DIR
from market_data.synthetic import (
    clean_daily_bars,
    clean_minute_bars,
    dirty_minute_bars,
)


def main() -> None:
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    clean_minute_bars().write_csv(FIXTURES_DIR / "clean_minute.csv")
    clean_daily_bars().write_parquet(FIXTURES_DIR / "clean_daily.parquet")
    dirty_minute_bars().write_csv(FIXTURES_DIR / "dirty_minute.csv")
    print(f"wrote fixtures to {FIXTURES_DIR}")
    for p in sorted(FIXTURES_DIR.iterdir()):
        print(f"  {p.name:24} {p.stat().st_size:>8} bytes")


if __name__ == "__main__":
    main()
