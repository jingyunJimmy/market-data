# Data Ingestion — `poetry run python scripts/ingest.py`

How to load CSV/Parquet market-data files into the local DuckDB store, and what
happens under the hood.

## 1. How the command is assembled

| Part | Meaning |
|---|---|
| `poetry run` | Runs the following command inside the Poetry-managed virtualenv (no manual `activate` needed). |
| `python scripts/ingest.py` | A small argparse entry point, [`backend/scripts/ingest.py`](../backend/scripts/ingest.py). It opens the DuckDB store, hands the paths to `IngestionService`, and prints the per-file tally. Run it from `backend/`. |

Discover the interface:

```bash
poetry run python scripts/ingest.py --help
```

It is deliberately thin — a shell over the same `IngestionService` the API and
the tests use, so there is no ingestion logic that only the script can reach.

## 2. Arguments

```
usage: ingest.py [-h] [--frequency {minute,daily}] paths [paths ...]
```

### `paths` (positional, required, repeatable)

Each entry may be a **file, a directory, or a glob pattern** — mixed freely.
Resolution lives in `expand_paths` in
[`backend/src/market_data/ingestion/readers.py`](../backend/src/market_data/ingestion/readers.py):

- **File** — used as-is.
- **Directory** — recursed (`rglob("*")`), every file underneath is included.
- **Glob** — a string containing `*`, `?`, or `[` that is not itself a real path;
  expanded relative to the current working directory.
- The final set is de-duplicated and sorted. If nothing matches, a
  `FileNotFoundError` is raised.

> **Quote your globs.** Otherwise the shell expands them before Python sees them.
> The shell also does not understand `**` the same way `pathlib` does.

### `--frequency` (optional)

Forces the bar frequency — `minute` or `daily`. When omitted, it is inferred
(see §5).

```bash
poetry run python scripts/ingest.py somefile.csv --frequency minute
```

## 3. What happens internally

Call chain: `main()` → `IngestionService.ingest_paths()` → per file
`ingest_file()` → `_run()`, in
[`backend/src/market_data/ingestion/pipeline.py`](../backend/src/market_data/ingestion/pipeline.py).

1. **Expand paths** — `expand_paths(paths)` produces the concrete file list.
2. **Per file:**
   - `reader_for(path)` picks a reader by extension (`.csv` / `.txt` →
     `CsvReader`; `.parquet` / `.pq` → `ParquetReader`). An unknown extension
     raises `UnsupportedFileError`.
   - `.read(path)` loads a raw Polars frame and appends the `__source_file` /
     `__row_number` bookkeeping columns (used for reject reporting). CSVs are
     read entirely as text so malformed values survive to normalisation.
   - `normalize(...)`:
     - `detect_frequency` decides minute vs daily.
     - Vendor column names are mapped onto the canonical schema.
     - A `ts` column is built (minute: naive Chicago wall clock localised to
       `America/Chicago`, then converted to tz-aware UTC; daily: the epoch-ms or
       `date` label stamped UTC as-is). Unparseable or DST-invalid values become
       `null` and are rejected with a reason.
     - Exact duplicate rows are collapsed (`rows_deduplicated`).
     - Rows that agree on `(contract, frequency, ts)` but disagree on the values
       are resolved: **the latest row in the file wins**, and the loser is
       returned in full (`rows_superseded`). See §9.
     - Bad rows are dropped **with a reason** (`rejections_by_reason`).
   - `repo.insert_bars(clean, run_id=...)` upserts into DuckDB, so an instant
     already stored is overwritten by the incoming row. Re-ingesting unchanged
     data stores nothing new and reports `+0 bars`.
   - `repo.record_rejections(...)`, `repo.record_superseded(...)` and
     `repo.record_run(...)` persist the rejected rows, the displaced rows, and
     the run metadata.
3. **Print a summary** — one line per file plus a total:

```
GCM26.parquet: +12345 bars, 7 rejected {'unparseable_timestamp': 5, 'unparseable_price': 2}, 4 superseded

Total: 12345 ingested, 7 rejected, 4 superseded (see superseded_bars)
```

`rejected` and `superseded` are separate counts on purpose: a rejected row was
malformed, a superseded row was valid but lost the one-bar-per-instant
tie-break. Only the first is a data-quality figure.

### Rejection reasons

Every reason a row can fail normalisation, in the order the classifier tries
them (first match wins):

| reason | meaning |
|---|---|
| `missing_required_columns` | the whole file has no OHLC columns *and* no timestamp column — every row is rejected with this one reason |
| `missing_contract` | no contract symbol after alias mapping |
| `unparseable_price` | an OHLC field was present but non-numeric (e.g. `"12.5x"`) |
| `missing_ohlc` | any of open/high/low/close is null |
| `ambiguous_or_nonexistent_local_time` | the minute wall clock fell in a DST spring-forward gap or fall-back overlap |
| `unparseable_timestamp` | no timestamp could be parsed at all |

Rows that are *suspicious but usable* — `high < low`, negative volume,
price ≤ 0, a minute bar with no volume — are **not** rejected here. They are
stored and flagged by the quality layer; see
[data-quality.md](data-quality.md).

## 4. Cleaning rules (normalisation)

All of it lives in one place —
[`backend/src/market_data/ingestion/normalize.py`](../backend/src/market_data/ingestion/normalize.py).
The readers deliberately know nothing about market data, so every cleaning
decision below is applied identically to CSV and Parquet, to the API, and to the
tests. The guiding rule:

> **Repair what is unambiguous, reject what cannot form a bar, keep and flag
> what is merely suspicious.** Nothing is silently patched, clipped or
> interpolated — a row is either fixed by a rule stated here, dropped with a
> reason, or stored as-is and reported by the quality layer.

### 4.1 File shape

| situation | rule |
|---|---|
| Unknown extension | `UnsupportedFileError` — the file is never guessed at. |
| CSV with mixed or dirty types | Read entirely as **text** (`infer_schema_length=0`), so malformed values reach normalisation intact instead of being coerced or nulled by the parser. |
| CSV ragged lines (too many / too few fields) | Tolerated: extra fields are trimmed, the file is not aborted. |
| CSV "missing" spellings — `""`, `NA`, `N/A`, `null`, `NULL`, `NaN` | All read as null on the way in, so downstream code only tests `is_null()`. |
| No OHLC columns **and** no timestamp column | Not bar data. The whole file is rejected with the single reason `missing_required_columns` rather than limping on row by row. |

### 4.2 Column names

Vendor headers are mapped onto the canonical schema **case-insensitively**, first
alias wins (`_COLUMN_ALIASES`):

| canonical | accepted aliases |
|---|---|
| `contract` | `contract`, `contract_symbol`, `symbol`, `ticker`, `instrument` |
| `root` | `root`, `product_root`, `product`, `root_symbol` |
| `exchange` | `exchange`, `venue`, `mic`, `operating_mic` |
| `open` / `high` / `low` | `open`/`o`/`open_price`/`px_open`, and the same pattern for high and low |
| `close` | `close`, `c`, `close_price`, `px_close`, **`settle`, `settlement`** |
| `volume` | `volume`, `v`, `vol`, `qty`, `quantity` |
| `open_interest` | `open_interest`, `oi`, `openinterest` |

`settle`/`settlement` folding into `close` is the one opinionated alias: daily
futures files often publish a settlement price instead of a last trade, and the
read side treats it as the session close.

A canonical column that no alias matched is **materialised as all-null** rather
than left absent, so every later expression sees a complete frame and a missing
column degrades into missing *data* (reportable) instead of a crash.

### 4.3 Values

| situation | rule | consequence |
|---|---|---|
| Padded numbers / symbols (`" 2431.5 "`) | Whitespace stripped before parsing | cleaned |
| Non-numeric price in a **non-empty** cell (`"12.5x"`, `"n/a "`) | Cast fails → row rejected | `unparseable_price` |
| Price cell empty or null | Not a parse failure, just absent | `missing_ohlc` if any of O/H/L/C is null |
| Non-numeric `volume` / `open_interest` | Cast to `Int64` non-strict → **null**, row kept | volume is not part of the bar's identity, so a bad size must not cost a valid price bar; nullness is visible in the quality report |
| Float representation noise | Prices rounded to 10 dp **only for the identity hash**, never in stored values | stable `row_hash` across files |
| Empty contract string | Normalised to null → rejected | `missing_contract` |
| Missing `root` | Derived from the contract by stripping the futures month code and year (`^([A-Za-z0-9]+?)[FGHJKMNQUVXZ]\d{1,2}$`), e.g. `GCM26` → `GC`, `CLZ25` → `CL`; if that fails, the first two characters | filled |
| Missing `exchange` | Filled with `""`, never rejected | the field is provenance, not part of a bar |

`high < low`, negative volume, non-positive prices and minute bars with no
volume are **not** touched here — see §4.7.

### 4.4 Timestamps — the one rule that differs by frequency

This dataset splits timestamp semantics between its two frequencies, and getting
it wrong shifts a whole minute series by five or six hours. So:

| frequency | source | rule |
|---|---|---|
| **daily** | `date`, else `timestamp_ms`, else `timestamp` | The label is already **UTC midnight**. It is *stamped* UTC (`replace_time_zone`) — attach the zone, do not shift the digits. Negative epochs (pre-1970) are valid. |
| **minute** | `timestamp_chicago_wall`, else `timestamp` / `datetime` | The source is a **tz-naive Chicago wall clock**. It is *localised* to `America/Chicago` (digits unchanged, the correct seasonal offset applied) and then *converted* to UTC (digits shift, instant preserved). The file's integer `timestamp_ms` is deliberately ignored for minute bars — the dataset's own docs say it is a label, not a UTC instant. |

Why localise-then-convert instead of subtracting a fixed offset: the same wall
time is `UTC-6` in winter and `UTC-5` in summer, and only the timezone database
knows which applies on a given date.

DST edge cases are resolved as rejections, not guesses:

| situation | rule |
|---|---|
| Wall clock inside the spring-forward gap (that local time never existed) | `ts` → null → rejected as `ambiguous_or_nonexistent_local_time` |
| Wall clock inside the fall-back overlap (that local time happened twice) | same reason — picking one of the two instants would invent data |
| No timestamp parsed at all | `unparseable_timestamp` |

The two are kept distinct on purpose: the parsed wall clock is retained
internally, so "a valid time that DST makes impossible" is reported differently
from "a string that was never a time".

**`trading_date`** is the bar's **Chicago calendar date**, not its UTC date.
It is taken from the file's own `trading_date` (or a daily `date`) when present;
otherwise, for minute bars, `ts` is converted **back** to the exchange timezone
(`MARKET_DATA_DAILY_BAR_TZ`, `America/Chicago`) before taking the date, so a
Chicago evening is not split at midnight UTC. The
dashboard's date filter, candles and displayed times all use this same Chicago
calendar.

### 4.5 Duplicates

Two different situations, two different rules — and neither loses data silently:

| situation | rule | recorded as |
|---|---|---|
| Byte-identical rows within one file (same contract, instant, O/H/L/C, volume) | Collapsed to one. Nothing is lost, so they are only counted. | `rows_deduplicated` |
| Rows agreeing on `(contract, frequency, ts)` but **disagreeing** on the values | **Last write wins**, ordered by the reader's `__row_number` so the outcome is deterministic. The loser is kept in full with the winner's `winning_row_hash`. | `superseded_bars`, reason `superseded_in_file` |
| The same instant arriving in a later file or run | Upsert overwrites; the displaced row is preserved | `superseded_bars`, reason `superseded_by_run` |

Rationale and how to change the tie-break: §9 and
[data-quality.md §2b](data-quality.md#2b-conflicting-duplicates--same-contract-frequency-ts-different-ohlcv).

### 4.6 Rejection vs. flagging — where the line is

A row is **rejected** only when it cannot form a bar at all: no contract, no
usable instant, or an incomplete/unparseable OHLC quad. Everything else is
stored. The full ordered list of reasons is in §3.

Rejections are never dropped on the floor — every one is persisted to
`rejected_rows` with its source file, row number and reason, so the count in the
summary can always be drilled into.

### 4.7 What is deliberately *not* cleaned

These are real defects, but each one is a judgement call that ingestion has no
business making:

| observed | why it is stored, not fixed | flagged as |
|---|---|---|
| `high < low` | The bar is internally inconsistent, but which of the two fields is wrong is unknowable. Swapping them would fabricate a price. | `high_lt_low` (error) |
| `open` or `close` outside `[low, high]` | On a daily bar this is often legitimate — a settlement price is computed, not traded — so the same predicate cannot mean "broken" everywhere. Severity is frequency-aware instead. | `open_out_of_range`, `close_out_of_range` |
| Price ≤ 0, or `inf` / `nan` | Clipping to a floor or dropping the row would either invent a number or hide a whole session. | `non_positive_price`, `non_finite_price` (error) |
| Negative volume | A count with no reading, but the price side of the bar may be perfectly good. | `negative_volume` (error) |
| Minute bar with no volume | Often a genuinely quiet minute rather than a defect, so it cannot be rejected — but on minute data it is worth a look. | `missing_volume` (warning, minute only) |
| Extreme jumps, spikes, stale prints | Outlier detection belongs to analysis, where the threshold is visible and tunable, not baked into storage. | the outlier checks |
| Missing bars / session gaps | A gap is an observation about a series, not a property of a row. Nothing can be filled in without inventing prices. | the gap checks |
| Timezone-shifted-looking data | Already handled correctly in §4.4. Re-guessing per row would undo it. | — |

Every one of them is detected and scored by the quality layer instead, with a
severity and a pointer back to the row —
see [data-quality.md](data-quality.md). The net effect is that the store stays
faithful to the source, and each cleaning decision is either reversible
(`superseded_bars`), traceable (`rejected_rows`), or explicit (this section).

## 5. How frequency is inferred

`detect_frequency` in
[`backend/src/market_data/ingestion/normalize.py`](../backend/src/market_data/ingestion/normalize.py),
in priority order:

1. An explicit `--frequency` wins.
2. Path hint — `/minute/` in the path → minute; `/daily/` → daily. The dataset's
   own directory layout follows this (e.g.
   `backend/data/raw/data/minute/COMEX/GC/...`).
3. Column names — `minute_of_day` or `timestamp_chicago_wall` → minute;
   `open_interest` → daily.
4. Fallback → minute.

So ingesting straight from the dataset's raw directory tree gets the frequency
right without `--frequency`.

## 6. Where the data lands

Four tables, defined in
[`schema.sql`](../backend/src/market_data/storage/schema.sql):

| table | holds |
|---|---|
| `bars` | canonical bars, **exactly one row per `(contract, frequency, ts)`** |
| `rejected_rows` | pointers to rows that could not form a bar (file, row number, reason) |
| `superseded_bars` | valid rows displaced by last-write-wins, in full, with the `winning_row_hash` |
| `ingestion_runs` | one row per run: counts, timing, contracts touched |

The DuckDB file path comes from `db_path` in
[`backend/src/market_data/config.py`](../backend/src/market_data/config.py), default:

```
<repo>/backend/data/market_data.duckdb
```

Override via environment variable or `.env` (prefix `MARKET_DATA_`):

```bash
MARKET_DATA_DB_PATH=/tmp/md.duckdb poetry run python scripts/ingest.py ...
```

Or copy [`.env.example`](../backend/.env.example) to `.env` and edit it.

## 7. Worked examples

All of these run from `backend/`; `data/` sits directly inside it, so the paths
below are relative to `backend/`.

```bash
# 1. Get data (downloads a subset from HuggingFace: CL, ES, GC)
poetry run python scripts/fetch_data.py
#   the full set:      poetry run python scripts/fetch_data.py --all
#   or chosen roots:   poetry run python scripts/fetch_data.py --roots VX ZN

# 2. Ingest every parquet file (minute + daily; frequency auto-detected)
poetry run python scripts/ingest.py "data/raw/data/**/*.parquet"

# Ingest one directory (recursive)
poetry run python scripts/ingest.py data/raw/data/minute/COMEX/GC

# A single file
poetry run python scripts/ingest.py data/raw/data/daily/COMEX/GC/GCM26.parquet

# Several inputs at once
poetry run python scripts/ingest.py data/raw/data/minute data/raw/data/daily

# Exercise the reject path with the bundled dirty fixture
poetry run python scripts/ingest.py data/fixtures/dirty_minute.csv
```

Then query it over the API:

```bash
poetry run market-data-serve --port 8000                  # in another terminal

curl "http://127.0.0.1:8000/contracts"
curl "http://127.0.0.1:8000/analytics/daily-ohlcv?contract=GCM26&source=minute"
curl "http://127.0.0.1:8000/quality/report?contract=GCM26&frequency=minute"
```

`contract` is required on both endpoints; `start` and `end` are optional and
default to the contract's full stored range. The Angular dashboard reads the
same endpoints.

## 8. Re-ingesting

- **Idempotent for unchanged data** — ingesting the same file again overwrites
  each instant with identical values, so nothing is added and the summary
  reports `+0 bars`, `0 superseded`.
- **A later file wins.** Because the upsert targets
  `(contract, frequency, ts)`, re-ingesting a *corrected* file replaces the
  stored bar. The old row is preserved in `superseded_bars` with reason
  `superseded_by_run`, so a correction is visible and reversible rather than
  silent. It counts as `+0 bars`, since the instant already existed.
- Exact duplicate rows *within* a file are collapsed during normalisation and
  counted in `rows_deduplicated`.
- Every run persists a run record, the detail of every rejected row, and every
  displaced row, so you can trace both why a row did not make it in and what a
  stored bar replaced.

## 9. One bar per instant

`bars` holds exactly one row per `(contract, frequency, ts)`. This is enforced,
not merely expected: it is the primary key, and the read side depends on it.
Aggregating daily candles from duplicated instants double-counts volume and
lets a bad print win the session high; a VWAP series with repeated timestamps
is rejected outright by the charting library.

Conflicts are therefore resolved at ingest by a **last-write-wins** rule, in two
scopes:

| scope | how | reason recorded |
|---|---|---|
| within one file | `_resolve_instant_conflicts` dedupes with `keep="last"`, ordered by the reader's `__row_number` so the outcome is deterministic | `superseded_in_file` |
| across files and runs | `INSERT … ON CONFLICT (contract, frequency, ts) DO UPDATE` | `superseded_by_run` |

Every displaced row is written to `superseded_bars` with all its values, its
provenance, and the `winning_row_hash` of the row that replaced it. The quality
report surfaces them as `resolved_instant_conflict` (WARNING), so resolving a
conflict never hides it. The rationale and how
to change the rule are in
[data-quality.md §2b](data-quality.md#2b-conflicting-duplicates--same-contract-frequency-ts-different-ohlcv).

> **Existing databases.** The `bars` primary key changed from `row_hash` to
> `(contract, frequency, ts)`, and `CREATE TABLE IF NOT EXISTS` cannot alter an
> existing table. `initialise()` detects the old key and raises with
> instructions: delete `backend/data/market_data.duckdb` and re-ingest. It is a
> rebuildable artefact.
