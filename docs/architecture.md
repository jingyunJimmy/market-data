# Architecture

## Goals

The brief asks for a *business-facing* application with **clean architecture**,
**extensible design**, **good test coverage**, and **thoughtful edge-case
handling**. The design optimises for those, in that order.

## Repository layout

One folder per concern, so the top level says what the system is made of:

```
market-data/
├── backend/     the Python service: FastAPI, one installable package
│   └── data/    raw/ (downloaded dataset) · fixtures/ (committed) · the DuckDB store
├── frontend/    the Angular dashboard
├── docs/        this file, plus notes on ingestion, charts, quality, insights, testing
└── .github/     CI
```

`data/` sits **inside** `backend/` because everything that writes or reads it is
backend code: `scripts/fetch_data.py` fills `data/raw/`, `scripts/ingest.py`
builds `data/market_data.duckdb`, and the tests read `data/fixtures/`.
`backend/src/market_data/config.py` resolves all three paths from `BACKEND_ROOT`,
so nothing else hard-codes a location. Only `data/fixtures/` is committed; the
download and the database are rebuildable and git-ignored.

Ingestion is a subpackage (`backend/src/market_data/ingestion/`) rather than a
separate top-level project: it depends on the canonical schema and the
repository port, so splitting it out would need a third shared package for
those, and three `pyproject.toml` files to express what one import already says.

## The four modules

The system is **four modules** over one canonical schema and one store. Each
module owns a vertical slice — its own domain logic, its own application
service, its own endpoints, and (for three of them) its own dashboard section —
so a change to gap detection never touches charting, and a change to charting
never touches ingestion.

| # | module | responsibility | backend code | frontend code |
|---|---|---|---|---|
| ① | **Data ingestion** | download the dataset · normalise to the canonical schema · store it, with an audit trail | `scripts/fetch_data.py`, `ingestion/` (`readers.py`, `normalize.py`, `pipeline.py`), `storage/` (`duckdb_repo.py`, `schema.sql`), `scripts/ingest.py` | — (offline, CLI only) |
| ② | **Dashboard presentation** | the two charts and the filters that drive them | `domain/analytics/` (`daily_bars.py`, `vwap.py`), `services/analytics_service.py`, `api/routes/analytics.py`, `api/routes/contracts.py` | `core/store.ts`, `core/api.ts`, `analytics/analytics.ts`, `shared/chart.ts`, `shared/stat.ts` |
| ③ | **Data quality validation** | gaps · missing timestamps · duplicates · values · outliers | `domain/quality/` (`checks/`, `rules.py`, `report.py`, `missing.py`, `details.py`), `services/quality_service.py`, `api/routes/quality.py` | `quality/quality.ts` |
| ④ | **Intelligent insights** | recurring quality patterns · suggested cleansing and validation rules, on request | `domain/insights/` (`evidence.py`, `models.py`, `verify.py`, `catalogue.py`), `llm/` (`claude.py`, `prompts.py`), `services/insights_service.py`, `api/routes/insights.py` | `quality/insights.ts` |
| — | *shared foundation* | canonical schema, settings, the storage port | `domain/models.py`, `config.py`, `storage/repository.py` | `core/models.ts`, `core/format.ts`, `core/time.ts` |

All backend paths are under `backend/src/market_data/`, all frontend paths
under `frontend/src/app/`.

### High-level view

```
                        Hugging Face futures dataset
                                     │
┌────────────────────────────────────▼────────────────────────────────┐
│ MODULE 1   DATA INGESTION     offline, CLI-driven, the only writer  │
│                                                                     │
│   download ──────────▶ normalise ──────────▶ store                  │
│   scripts/fetch_data.py   ingestion/normalize.py   storage/         │
└────────────────────────────────────┬────────────────────────────────┘
                                     │
                        ┌────────────▼───────────┐
                        │  DuckDB                │
                        │  bars + audit tables   │
                        └────┬──────────────┬────┘
                  read-only  │              │  read-only
              ┌──────────────┘              └──────────────┐
              │                                            │
┌─────────────▼───────────────┐      ┌─────────────────────▼────────┐
│ MODULE 2                    │      │ MODULE 3                     │
│ DASHBOARD PRESENTATION      │      │ DATA QUALITY VALIDATION      │
│   daily OHLCV, rolling VWAP │      │   gaps, missing timestamps,  │
│   filtering                 │      │   duplicates, values,        │
│                             │      │   outliers                   │
│   services/analytics_service│      │   services/quality_service   │
└──────────────┬──────────────┘      └───────┬─────────────┬────────┘
               │                             │             │ same bars, same validator,
               │                             │             │ every occurrence
               │                             │ ┌───────────▼──────────────────┐
               │                             │ │ MODULE 4                     │    ┌───────────────┐
               │                             │ │ INTELLIGENT INSIGHTS         │    │ Claude        │
               │                             │ │   on request: evidence →     │───▶│ via LangChain │
               │                             │ │   patterns → suggested rules │◀───│ llm/claude.py │
               │                             │ │                              │    └───────────────┘
               │                             │ │   services/insights_service  │     aggregates out,
               │                             │ └───────────┬──────────────────┘     JSON back
               └──────────────┬──────────────┴─────────────┘
                              │
                 ┌────────────▼────────────┐
                 │ FastAPI REST            │
                 │ api/routes/             │
                 └────┬───────────────▲────┘
                 JSON │               │ filters: contract, frequency,
                      │               │ date range, VWAP window;
                      │               │ the "Generate insights" button
                 ┌────▼───────────────┴────┐
                 │ Angular dashboard       │
                 │ charts tab · quality tab│
                 └─────────────────────────┘
```

Read it top to bottom: ingestion runs **once, offline**, and is the only writer;
the other three modules are **read-only** consumers of the same store, reached
through the same API, and the dashboard's filters are the one thing that flows
backwards.

Module ④ sits on top of module ③: it loads the same bars, runs the same
validator over them with a far higher evidence cap, and reasons only about the
findings that come out. It is also the only module that reaches outside the
process — to Claude, with aggregated statistics and never bars — and only when
someone presses the button.

### How each module is layered

Every module is layered the same way, which is why they can be described once:

```
   ┌──────────────────────────────────────────────────────────────┐
   │ Presentation                                                 │
   │   Angular components · FastAPI routes · CLI scripts          │
   │   no business logic — they call services                     │
   └───────────────────────────────┬──────────────────────────────┘
                                   │
   ┌───────────────────────────────▼──────────────────────────────┐
   │ Application                                                  │
   │   IngestionService · AnalyticsService · QualityService       │
   │   InsightsService                                            │
   │   orchestration: filter inputs, call domain, shape outputs   │
   └──────────┬───────────────────────────────────┬───────────────┘
              │                                   │
   ┌──────────▼──────────────────┐   ┌────────────▼─────────────────┐
   │ Domain — pure, no I/O       │   │ Infrastructure — adapters    │
   │   models.py                 │◀──┤   ingestion/readers.py       │
   │   analytics/   quality/     │   │   storage/duckdb_repo.py     │
   │   insights/                 │   │   llm/claude.py              │
   │   defines the ports         │   │   implement those ports      │
   └─────────────────────────────┘   └──────────────────────────────┘
```

**Dependency rule:** arrows point inward. `domain/` imports nothing from
`ingestion/`, `storage/`, `llm/` or `api/`, and has no file, network, or DB access.

| Port (Protocol) | Location | Adapters | Used by |
|---|---|---|---|
| `FileReader` | `ingestion/readers.py` | `CsvReader`, `ParquetReader` | ① |
| `BarRepository` | `storage/repository.py` | `DuckDbRepository` | ①②③④ |
| `Rule` (callable) | `domain/quality/rules.py` | every function in `domain/quality/checks/` | ③ |

Swapping DuckDB for a warehouse or adding an Avro reader is a new adapter — no
change to services or domain. The LLM has no port of its own: `InsightsService`
takes `ClaudeClient` (`llm/claude.py`) directly, and the tests hand it any object
with the same `model` property and `complete_json` method. `IngestionService` is
an application service like the others; it just lives in `ingestion/` next to the readers and the
normaliser it drives, so the whole ingest path reads top to bottom in one
package.

---

## ① Data ingestion

Download → normalise → store. Driven by two CLI scripts, never by the API.

```
 scripts/fetch_data.py ──▶ backend/data/raw/**.parquet ──▶ scripts/ingest.py
   snapshot_download          (git-ignored)                 expand the paths,
                                                            build the service
   │
   ▼
   IngestionService.ingest_file      ingestion/pipeline.py
   │
   ▼
   FileReader.read                   ingestion/readers.py
   CsvReader | ParquetReader         + source file and row-number
                                       bookkeeping
   │
   ▼  raw frame
┌──▼─────────────────────────────────────────────────────────────────┐
│ normalize()   ingestion/normalize.py                               │
│   1. column aliases       → canonical names                        │
│   2. frequency detection  → path hint, column signature, default   │
│   3. timestamps → UTC     → minute: Chicago wall clock localised   │
│                             daily:  date / epoch-ms stamped UTC    │
│   4. lenient OHLCV coercion                                        │
│   5. structural rejection vs. flagging                             │
│   6. row_hash, one bar per instant, last write wins                │
└──────┬──────────────────────┬─────────────────────────┬────────────┘
       │ clean canonical bars │ rejected rows + reason  │ superseded rows
       ▼                      ▼                         ▼
 insert_bars (upsert)   rejected_rows (table)     superseded_bars (table)
 storage/duckdb_repo.py                           + winning_row_hash
       │                                            ▲
       ▼                                            │
  bars (table) ───────────── displaced row ─────────┘

 IngestionService ────────── run lineage ──────▶ ingestion_runs (table)
```

`normalize()` is the single place every messy real-world detail is handled:

- **Column mapping** — `contract_symbol→contract`, `settle→close`, … via an
  alias table (case-insensitive).
- **Frequency detection** — path hint (`/minute/`), then column signature
  (`minute_of_day`, `timestamp_chicago_wall`, `open_interest`), then default.
- **Timestamps** — branch on frequency. Minute: parse the naive Chicago wall
  clock, localise to `America/Chicago` with `ambiguous="null"` /
  `non_existent="null"`, convert to UTC; the null cases become explicit
  rejections (`ambiguous_or_nonexistent_local_time`). Daily: `date` / epoch-ms
  at 00:00 UTC, stamped UTC rather than converted.
- **Type coercion** — OHLCV parsed leniently; a value that was present but
  unparseable is captured (`unparseable_price`), not silently nulled.
- **Rejection vs. flagging** — ingestion rejects only *structurally unusable*
  rows (no contract, unparseable price, missing OHLC, no usable timestamp). Rows
  that are *suspicious but usable* (`high < low`, negative volume, price ≤ 0)
  are kept and left for module ③. This keeps the two concerns separate.
- **One bar per instant** — `(contract, frequency, ts)` is the primary key, and
  writes upsert (`ON CONFLICT … DO UPDATE`). Rows that agree on the instant but
  disagree on the values are resolved by **last write wins**, within a file and
  across runs. Every displaced row is kept in full in `superseded_bars` with the
  `winning_row_hash`, and reported as `resolved_instant_conflict` (WARNING), so
  the resolution is auditable and never silent.
- **Idempotency** — each row also gets a SHA-1 `row_hash` over
  `(contract, frequency, ts, OHLC, volume)`. It collapses byte-identical rows
  inside a file and tells a correction apart from a re-ingest: identical values
  overwrite a row with itself and log nothing, so re-running a file is a no-op.

---

## ② Dashboard presentation

The filter bar, the two charts, and the calculations behind them. The Angular
app computes **nothing the backend could compute**: it holds filter state and
renders what the API returns.

```
┌─ frontend/src/app/ ─────────────────────────────────────────────────┐
│  filter bar (app.ts)                                                │
│    contract · frequency · start · end · VWAP window                 │
│                     │ writes signals                                │
│                     ▼                                               │
│  DashboardStore (core/store.ts) — 5 writable signals, all the state │
│                     │ derives each request                          │
│                     ▼                                               │
│  MarketDataApi (core/api.ts) — one httpResource per endpoint        │
└─────────────────────┬───────────────────────────────────────────────┘
                      │ HTTP   (/api/* proxied to FastAPI in dev)
                      ▼
┌─ backend/src/market_data/ ──────────────────────────────────────────┐
│  api/routes/contracts.py   GET /contracts                           │
│  api/routes/analytics.py   GET /analytics/daily-ohlcv               │
│                            GET /analytics/vwap                      │
│                     │                                               │
│                     ▼                                               │
│  AnalyticsService   services/analytics_service.py                   │
│                     │                                               │
│                     ▼                                               │
│  DuckDbRepository.load_bars — contract, frequency and date range    │
│                     │         filtered in SQL, not in the browser   │
│          ┌──────────┴──────────┐                                    │
│          ▼                     ▼             domain/analytics/      │
│     daily_ohlcv()         rolling_vwap()     pure, no I/O           │
└──────────┬─────────────────────┬────────────────────────────────────┘
           │ DailyBar[]          │ VwapSeries (+ truncated flag)
           ▼                     ▼
┌─ frontend/src/app/ ─────────────────────────────────────────────────┐
│  Analytics panel (analytics/analytics.ts)                           │
│    candles     ──▶ Chart (shared/chart.ts)   OHLC candles + volume  │
│    vwapSeries  ──▶ Chart (shared/chart.ts)   rolling VWAP line      │
│    lastClose, totalVolume, rangeChange ──▶ Stat (shared/stat.ts)    │
└─────────────────────────────────────────────────────────────────────┘
```

**The two charts and what computes them**

| chart | calculation | where |
|---|---|---|
| Daily OHLC candles + volume | group by `(contract, trading_date)`: first open, max high, min low, last close, summed volume, last open interest, and a `bar_count`. Already-daily input passes through with `bar_count = 1`, so the same endpoint serves both an aggregation of minute bars and the vendor's own daily bars. | `domain/analytics/daily_bars.py` → `AnalyticsService.daily_bars` → `analytics.ts` `candles` (candle and volume series) |
| Rolling VWAP line | time-based trailing window (default 15 min) per contract, `Σ(typical_price·volume) / Σ(volume)` with `typical_price = (H+L+C)/3`. Zero-volume windows yield `null`, never a divide-by-zero. | `domain/analytics/vwap.py` → `AnalyticsService.vwap` → `analytics.ts` `vwapSeries` |

**Filtering** happens in exactly two places, and neither is the browser:
`DuckDbRepository.load_bars` turns contract / frequency / date range into a SQL
`WHERE`, so only the needed slice is materialised, and `AnalyticsService.vwap`
then caps the series at `limit` points and sets `truncated`, because a wide
range on a liquid contract is hundreds of thousands of points.

On the client, `DashboardStore` is the only state. The five filters are writable
signals; the `httpResource`s derive their requests from them, so changing a
filter refetches and nothing else has to coordinate. Selecting a contract resets
the date range to a window that actually contains data, read from `/contracts` —
30 days on minute, a year on daily. The VWAP request is withheld entirely on
`daily`, where a trailing intraday window is undefined.

Everything the client renders is in **Chicago time**. The API returns UTC;
`core/time.ts` converts at display time, and one note under the page title says
so. Chicago is the calendar `trading_date` and the date filter already use, so
charts and filters agree on where a day starts.

```
frontend/src/app/
  core/     models.ts  API response types (mirror the Pydantic models)
            api.ts     the one place endpoint URLs live; returns httpResource
            store.ts   DashboardStore -- filter signals + the resources
            format.ts  number formatting
            time.ts    Chicago-time display, and the shift that puts charts in it
  shared/   chart.ts   lightweight-charts lifecycle, series in / canvas out
            stat.ts    one headline figure
  analytics/  quality/  the two panels; quality/ also holds the insights section
  app.*       shell: header, filter bar, tabs
```

Charts are [lightweight-charts](https://tradingview.github.io/lightweight-charts/)
v5 — purpose-built for OHLC and time series, and about 60 kB gzipped rather than
the megabytes a general-purpose plotting library costs. The whole production
bundle is 366 kB raw / 104 kB over the wire.
[dashboard-charts.md](dashboard-charts.md) covers each chart and the VWAP
computation in detail.

In development the Angular dev server proxies `/api/*` to the FastAPI service
(`frontend/proxy.conf.json`, rewriting the prefix away), which keeps the browser
on one origin and means the backend carries no CORS configuration.

---

## ③ Data quality validation

A **list of small rules**, each a pure `(RuleContext) -> Iterable[QualityIssue]`.
`all_rules()` names every one of them explicitly; `QualityValidator` runs them in
order and sorts the results by severity. Adding a check = one self-contained
function plus its name in that list.

```
 Quality panel (quality/quality.ts) — same filters, severity totals, paging
          │
          ▼
 api/routes/quality.py    GET /quality/report
                          GET /quality/missing-timestamps
                          GET /quality/issue-details
          │
          ▼
 QualityService   services/quality_service.py
          │
          ├── load_bars() ─────────────┐
          │                            │
          └── load_superseded() ───────┤  conflicts resolved at ingest are
                                       │  not in `bars`, so the report
                                       ▼  loads the displaced rows too
                              RuleContext   domain/quality/rules.py
                              bars + superseded + filters + settings
                                       │
 ┌──────────────────┬──────────────────┼─────────────────┐
 ▼                  ▼                  ▼                 ▼
 duplicates.py      gaps.py            outliers.py       values.py
 exact_duplicates   timestamp_gaps     price_return_     invalid_values
 conflicting_dups   modal interval,    outliers,         high below low,
 resolved_instant_  active period,     volume_spikes     negative volume,
 conflicts          intra / extended   median-MAD on     price ≤ 0, close
                    / session_break,   log returns,      outside [low,high]
                    daily: missing_    50-bar rolling    severity depends
                    sessions           median            on frequency
 └──────────────────┴─────────┬────────┴─────────────────┘
                              ▼
                QualityValidator.validate   domain/quality/report.py
                runs all_rules() in order, sorts by severity
                              │
                              ▼
                      QualityReport ──▶ GET /quality/report

 two side paths, kept off the report because of volume:
   load_bars ──▶ missing_timestamps()  domain/quality/missing.py
                    ──▶ GET /quality/missing-timestamps  (paged instants)
   rule evidence ──▶ details.py ──▶ GET /quality/issue-details (paged rows)
```

| check | notable edge-case handling |
|---|---|
| `duplicates` | conflicts are resolved at ingest, so this rule reports them from the audit trail (`resolved_instant_conflict`, warning) rather than from `bars`; the exact/repeated-timestamp rules remain for validating a raw frame |
| `values` | **severity is context-aware**: daily `close` outside `[low,high]` is `INFO` (settlement); the same on a minute bar is `ERROR` |
| `gaps` | infers modal bar interval per contract, restricts to the **active period** (first `volume>0`), classifies each gap as `intra_session_gap` / `extended_gap` / `session_break`; only the first two are warnings. Daily: business-day diff → `missing_sessions` |
| `outliers` | median/MAD on log returns (robust to the very spikes it looks for); volume spikes vs a 50-bar rolling median |
| *missing timestamps* | not a rule but a separate endpoint: the report summarises gaps in the thousands, the instants inside them number in the hundreds of thousands, so they are enumerated and paged on their own |

Every `QualityIssue` carries `category`, `code`, `severity`, contract,
frequency, a timestamp span, a count, and a `context` dict. The two endpoints
beyond `/report` exist because of volume: `missing-timestamps` enumerates every
absent instant, and `issue-details` pages past the sample of evidence the report
carries.

---

## ④ Intelligent insights

On request, reads the quality findings for **recurring patterns** and proposes
**cleansing or validation rules**. The division of labour is the design: code
computes the evidence, an LLM draws conclusions from it, and code checks that
each conclusion is well-formed before it is shown — beside the evidence it
cites, so a reader can judge the reasoning.
[insights.md](insights.md) has the detail.

```
 Insights section (quality/insights.ts) — a button; nothing runs until it is pressed
          │
          ▼
 api/routes/insights.py   POST /insights
          │
          ▼
 InsightsService   services/insights_service.py
          │
          ├── load_bars() + load_superseded() ──▶ QualityValidator (module ③),
          │                                       evidence cap raised to
          │                                       insights_max_occurrences
          ▼
 build_evidence_pack()   domain/insights/evidence.py   pure
   per finding: exchange-time hours, days affected, weekdays, durations,
   magnitude, share after a gap, trend
          │
          ▼
 _identify_patterns ──▶ verify_patterns      domain/insights/verify.py
          │                schema only: fields, types, enum values
          ▼
 _suggest_rules     ──▶ verify_suggestions
          │                schema only: a catalogued type, a valid kind
          ▼
 InsightsReport: evidence · patterns · suggestions · what was rejected and why

 both calls: ClaudeClient.complete_json  llm/claude.py, Claude through LangChain
             prompts and JSON schemas     llm/prompts.py
```

`InsightsService` builds the prompt (`llm/prompts.py`), calls
`ClaudeClient.complete_json` and hands the parsed JSON straight to
`verify_patterns`/`verify_suggestions` — there is no adapter class in between.
Verification checks the shape of each item, not whether its figures match the
evidence; see [insights.md](insights.md#3-verification).

If the LLM produces no usable answer at all — unreachable, timed out,
unparseable — the route answers **503** with the reason, and the dashboard shows
it. An answer that is malformed in places is not a failure: verification
drops the malformed items, keeps the rest, and returns the refusals.

## Storage

DuckDB, embedded. Chosen because the analytics and gap detection are naturally
window-function SQL, it reads/writes Parquet and CSV natively, needs no server,
and hands results back as Arrow/Polars zero-copy. Four tables, defined in
`storage/schema.sql`:

| table | holds |
|---|---|
| `bars` | canonical bars, PK `(contract, frequency, ts)` — one row per instant |
| `rejected_rows` | pointers to structurally unusable rows (file, row number, reason) |
| `superseded_bars` | valid rows displaced by last-write-wins, in full, plus the `winning_row_hash` |
| `ingestion_runs` | lineage: one row per run |

Two write-side tables rather than one because the two cases are different
kinds of fact. A rejected row is malformed and never entered `bars`; a
superseded row was valid and lost a tie-break. Only the first belongs in a
data-quality count, and only the second is worth storing with its values.

`Polars` is used for row-level normalisation, analytics and synthetic data;
`DuckDB` owns persistence and set-based queries.

## Testing strategy

157 tests, `poetry run pytest` from `backend/`.

| layer | how |
|---|---|
| domain | exhaustive unit tests; a **Hypothesis** property test for the VWAP invariant (VWAP ∈ window `[low, high]`) |
| ingestion | committed dirty fixture (`backend/data/fixtures/dirty_minute.csv`) with a *known* set of injected problems — exact/conflicting dups, `high<low`, negative volume, non-positive price, unparseable timestamp/price, missing OHLC, a 20-min gap |
| services | in-memory DuckDB repository, real fixtures end-to-end: re-ingest idempotency, the one-bar-per-instant invariant, a simulated vendor correction across two runs, and that a resolved conflict still appears in the quality report |
| API | FastAPI `TestClient`, isolated temp DB per test: date bounds, aggregation, VWAP truncation, required parameters, and missing-timestamp paging |
| scripts | `scripts/ingest.py` driven end-to-end against a temp DB |
| insights | evidence statistics on hand-built reports (exchange time, daily dates, after-gap timing, caps); schema verification one malformed item at a time; the Claude client driven by a scripted LangChain model, never a live one; a planted daily halt taken through evidence, a scripted model, verification and the route |
| cross-check | computed daily bars re-derived against the minute bars they came from (`test_computed_daily_matches_the_minute_bars_it_came_from`) |

`scripts/make_fixtures.py` + `market_data/synthetic.py` are the single source of
truth for what "clean" and "dirty" data look like, shared by fixtures and tests.
The synthetic generator uses a tiny deterministic PRNG, so fixtures regenerate
byte-identically without a numpy dependency.

## Deliberate trade-offs

- **`float64` prices**, not `Decimal` — matches the source and the Polars/DuckDB
  fast path; there is a single documented place (`CANONICAL_SCHEMA`) to change it.
- **Gap detection infers the trading calendar** from the data rather than
  shipping exchange session/holiday calendars. Daily `missing_sessions` findings
  say so in their message, and swapping the business-day diff for a real
  calendar is a change to one function.
- **Conflicting duplicates are resolved by last-write-wins**, not left for the
  reader. One bar per instant is the invariant every read path depends on, so a
  survivor has to be picked at ingest; the rule assumes a later row is a
  correction. Every displaced row is kept in full in `superseded_bars` and still
  reported as a warning, so the choice is auditable and reversible. See
  [data-quality.md](data-quality.md#2-duplicate-records).
- **mypy is pragmatic, not `--strict`** — Polars' scalar accessors return very
  broad unions that make strict mode noisy without adding safety. Strict checks
  that catch real mistakes are kept; three Polars-heavy modules (`checks/gaps.py`,
  `checks/outliers.py`, `insights/evidence.py`) relax a few error codes with an
  explanatory override.
- **One DuckDB database, one cursor per thread.** FastAPI serves synchronous
  endpoints from a worker pool, and a single DuckDB connection object cannot
  serve concurrent queries — interleaved execute/fetch calls return each
  other's (or empty) results. `DuckDbRepository` hands every thread its own
  `cursor()` onto the same database, documented at the `_cursor` helper in
  `storage/duckdb_repo.py`.
- **Timestamps are pinned to UTC at the connection.** DuckDB materialises
  `TIMESTAMPTZ` in the session zone, so without `SET TimeZone = 'UTC'` the API's
  output would depend on which machine it runs on.
- **Stored in UTC, displayed in Chicago time.** The conversion lives only in
  `frontend/src/app/core/time.ts`, so the API stays zone-neutral. The chart
  library has no time zones, so the VWAP line is plotted at Chicago wall-clock
  seconds; the one wall-clock hour that repeats when daylight saving ends is
  dropped, which never touches CME data because it falls in the weekend closure.
  The display zone is a frontend constant that must match
  `MARKET_DATA_DAILY_BAR_TZ`.
- **Insights are generated on request, not with the report.** They read every
  occurrence behind every finding rather than the report's capped sample, and
  each run asks an LLM, which can take tens of seconds and cost money. The
  dashboard asks only when the button is pressed, and the store pins the request
  to the selection it was made for, so moving a filter never generates by itself.
- **Insights need a model; nothing else does.** There is no deterministic
  fallback: without a reachable endpoint the insights section reports why and
  offers nothing. The quality report the insights read, and the rest of the
  dashboard, work without one.
- **The LLM's reading is shown, not fact-checked.** It only ever sees
  aggregated evidence (no bars) and is asked to cite it by id, but verification
  checks only that each pattern and suggestion is well-formed. An invented
  figure gets through, which is why the dashboard renders each pattern's
  figures from the evidence rows it cites, beside the explanation rather than
  instead of it.

## Extending

| want to… | module | do this |
|---|---|---|
| support a new file format | ① | implement `FileReader`, add to `DEFAULT_READERS` |
| use a real database | ①②③④ | implement `BarRepository` |
| add an analytic | ② | add a pure function in `domain/analytics/` + a service method |
| add a dashboard section | ② | a component under `frontend/src/app/`, plus a resource in `store.ts` |
| add a quality check | ③ | write the function in `domain/quality/checks/`, add it to `all_rules()` |
| give the model more to reason from | ④ | add a field to `Evidence`, compute it in `domain/insights/evidence.py`, describe it in `llm/prompts.py`, and mirror it in `frontend/src/app/core/models.ts` |
| offer a new kind of suggested rule | ④ | add a `SuggestionType` and its entry in `domain/insights/catalogue.py` — the prompt lists the catalogue and the JSON schema is built from it, one variant per type with that type's params — then add it to `SuggestionType` and `TYPE_LABEL` in the frontend |
| use a different Claude model | ④ | set `MARKET_DATA_LLM_MODEL` |
| use a different LLM provider | ④ | write a client beside `llm/claude.py` with the same `model` property and `complete_json(system, user, schema)` method over that provider's LangChain chat model, build it in `api/deps.py`, and widen the `client` type `InsightsService` accepts |
