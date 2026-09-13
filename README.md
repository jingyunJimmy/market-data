# Futures Market Data — ingestion, analytics & data quality

A business-facing application that ingests historical futures market data
(CSV **or** Parquet), generates analytics, and validates data quality.

## The problem it solves

Vendor futures files arrive inconsistent and untrustworthy: different schemas
per frequency, ambiguous timestamps, repeated rows, and real market artefacts
that look like corruption. An analyst cannot answer "what did CL trade at last
Tuesday, and can I trust that number?" without first cleaning the data by hand.

This application closes that gap in four steps:

- **Ingestion** — normalise any vendor file to a canonical bar schema; capture
  malformed rows with reasons; collapse exact duplicates; store exactly **one
  bar per `(contract, frequency, ts)`**, resolving value conflicts by
  last-write-wins and keeping every displaced row for audit; idempotent
  re-ingest.
- **Analytics** — daily OHLCV bars and a rolling time-based 15-minute VWAP,
  filtered by contract and date range.
- **Data quality** — missing timestamps and data gaps (session-aware),
  duplicate records, conflicts resolved at ingest but still reported, invalid
  prices/volumes (severity-aware), and statistical outliers.
- **Intelligent insights** — on request, reads the quality findings for
  recurring patterns (a gap at the same hour every session, outliers that
  follow gaps, a feed that restates bars) and suggests cleansing or validation
  rules. An LLM reads statistics computed from the findings; each pattern is
  shown beside the statistics it cites, so a reader can check the reasoning.

The headline trap the design turns on: in this dataset a minute file's
`timestamp_ms` is **not** a UTC epoch, so reading it as one silently shifts
every intraday bar by hours. See [The dataset](#the-dataset).

Surfaces: an **Angular** dashboard over a **FastAPI** REST service, plus
ingestion and dataset scripts — all on the same application services.

## Layout

```
market-data/
├── backend/     Python service: FastAPI REST API
│   ├── src/market_data/
│   │   ├── ingestion/   file readers · normalisation · the ingest pipeline
│   │   ├── domain/      canonical models · analytics · quality rules · insights (pure, no I/O)
│   │   ├── storage/     repository port + DuckDB adapter
│   │   ├── llm/         Claude client (LangChain) + prompts for intelligent insights
│   │   ├── services/    orchestration
│   │   └── api/         HTTP routes
│   ├── scripts/         fetch the dataset · ingest files · regenerate fixtures
│   ├── data/            raw/ (downloaded) · fixtures/ (committed) · the DuckDB store
│   └── tests/
├── frontend/    Angular dashboard
├── docs/
└── .github/workflows/ci.yml
```

`data/` lives inside `backend/` so the dataset and the DuckDB store sit beside
the code that produces and reads them. `backend/src/market_data/config.py` is
the single place those locations are resolved.

## Prerequisites

| tool | version |
|---|---|
| Python | 3.11, 3.12 or 3.13 |
| Poetry | 2.0 or newer |
| Node.js | 22 |
| A Claude API key | only for intelligent insights — see [Setting up the Claude API key](#setting-up-the-claude-api-key) |

> **Intelligent insights need a Claude API key.** The analytics, charts and
> data-quality report all work without one, but the **Generate insights** button
> on the Data quality page calls Claude and shows an error until a key is
> configured. Follow [Setting up the Claude API key](#setting-up-the-claude-api-key)
> before trying it.

## Running the backend

All backend commands run from `backend/`.

```bash
cd backend
poetry install --all-extras
```

If you want intelligent insights, create `backend/.env` with your Claude API key
now, before starting the server. See
[Setting up the Claude API key](#setting-up-the-claude-api-key).

Then load data. **Start with the real dataset** — it is what the application was
built against, and the analytics and quality report only say something worth
reading on genuine vendor files. The dataset is public, so no Hugging Face token
is needed:

```bash
poetry run python scripts/fetch_data.py      # curated subset: CL, ES, GC
#   or: poetry run python scripts/fetch_data.py --all         # all 8 roots, 80 parquet files
#   or: poetry run python scripts/fetch_data.py --roots VX ZN

poetry run python scripts/ingest.py "data/raw/data/**/*.parquet"
poetry run market-data-serve                 # http://127.0.0.1:8000
```

The curated subset is the recommended starting point: three liquid roots, both
frequencies, enough history for the rolling VWAP and every quality check to have
something to work with. `--roots` takes any of the 8 roots listed under
[The dataset](#the-dataset); `--all` fetches the lot. Re-running the ingest is
safe — it is idempotent on `(contract, frequency, ts)`.

If you want to check the stack comes up before committing to a download, the
committed fixtures ingest in a second:

```bash
poetry run python scripts/ingest.py data/fixtures/dirty_minute.csv
poetry run market-data-serve
```

They are tiny and deliberately dirty — built to exercise the rejection and
conflict paths in tests, not to be looked at in the dashboard.

`market-data-serve` accepts `--host`, `--port` and `--reload`. Interactive API
docs are at `http://127.0.0.1:8000/docs`.

## Running the frontend

In a second terminal:

```bash
cd frontend
npm install
npm start                                    # http://localhost:4200
```

The dev server proxies `/api/*` to the API on port 8000, so the backend must be
running too. Ingest first — the dashboard only shows what is in the database.

To build the production bundle instead:

```bash
npm run build
```

## Running the tests

**Backend — pytest**, with [Hypothesis](https://hypothesis.readthedocs.io) for
property-based tests. Unit tests live in `backend/tests/unit`, end-to-end tests
in `backend/tests/integration`.

```bash
cd backend
poetry run pytest                            # the whole suite
poetry run pytest tests/unit                 # unit tests only
poetry run pytest -m integration             # end-to-end tests only
poetry run pytest tests/unit/test_quality.py # a single file
```

**Frontend — Vitest**, run through the Angular `@angular/build:unit-test`
builder on a jsdom environment. Specs sit next to the code they cover as
`*.spec.ts`.

```bash
cd frontend
npm test                                     # single run, no watch
npx ng test                                  # watch mode
```

[CI](.github/workflows/ci.yml) runs the backend lint, type check and tests on
Python 3.11, 3.12 and 3.13, and the frontend format check, tests and build on
Node 22.

[docs/testing.md](docs/testing.md) covers the testing strategy in full: the
frameworks, and the edge cases each layer was written to survive — daily OHLCV
aggregation, the rolling VWAP window, normalisation, the quality rules, and the
chart series the dashboard draws.

## The dashboard

Two sections over a shared filter bar — contract, frequency, start, end, and
the VWAP window:

- **Dashboard** — daily OHLCV candlesticks with volume bars beneath them, the
  rolling VWAP against the typical price, four headline figures, and a
  session-by-session table.
- **Data quality** — bars checked, counts by severity, and every finding with
  its severity, category, check code, occurrence count and timestamp span.
  Below the findings, **Intelligent insights** generates recurring patterns and
  suggested rules when you press its button — never on its own, because it
  reads every occurrence behind every finding and asks an LLM.

The frequency filter picks which stored series you are looking at: on `minute`
the daily bars are aggregated from intraday data and VWAP is available; on
`daily` the vendor's own daily bars are shown and validated, and the VWAP panel
explains why it is empty.

Every date and time on the page is shown in **Chicago time** (CT), stated once
under the title. The API and database stay in UTC; the conversion is display-only,
in `frontend/src/app/core/time.ts`, and the date filter uses the same Chicago
calendar as `trading_date`.

Try the dirty fixture to see the quality path light up. It carries deliberate
conflicts, so the ingest summary reports `4 superseded` alongside the rejects
and the report shows them as `resolved_instant_conflict`:

```bash
curl "http://127.0.0.1:8000/quality/report?contract=CL_TEST&frequency=minute"
```

See [docs/data-quality.md](docs/data-quality.md#2-duplicate-records).

## The scripts

All run from `backend/`.

```bash
poetry run python scripts/fetch_data.py                # download the dataset
poetry run python scripts/ingest.py <files|dirs|globs> # load into DuckDB
poetry run python scripts/make_fixtures.py             # regenerate test fixtures
```

`--help` on any of them lists its full interface. Everything else — the
catalogue, daily bars, VWAP, the quality report and insights — is served by the API.

## The dataset

`huggingface.co/datasets/lynx1231/historical-futures-data-sample` — 8 roots
(CL, ES, GC, SB, SR3, VX, ZC, ZN), daily + minute Parquet.

Two things about it drive the design (both documented in the dataset's
`schema.json`):

| | minute files | daily files |
|---|---|---|
| timestamp | `timestamp_chicago_wall` — tz-**naive** America/Chicago wall clock. `timestamp_ms` is *not* a UTC epoch here. | `timestamp_ms` **is** Unix-ms at 00:00 UTC (or `date`). |
| extras | `minute_of_day`, `trading_date` | `open_interest` |

Ingestion localises the minute wall-clock to `America/Chicago` (handling DST
spring-forward / fall-back), then stores everything in UTC. The dashboard converts
back and displays Chicago time.

Real quirks handled rather than mis-flagged: **settlement-only bars**
(`volume=0`, `O=H=L=C`), **daily `close` outside the day's `[low, high]`**
(legitimate settlement — only informational on daily bars), sparse pre-front-month
history, and pervasive legitimate overnight/weekend/holiday gaps.

## Loading data — two ways

1. **Bulk download** (`backend/scripts/fetch_data.py`) → `backend/data/raw/` via
   `huggingface_hub.snapshot_download`. This is what the app ingests.
2. **Application path** — `scripts/ingest.py <files|dirs|globs>` → readers →
   normalise → DuckDB. Everything downstream queries DuckDB.

## The API

| endpoint | returns |
|---|---|
| `GET /contracts` | catalogue: contract, root, exchange, frequency, date bounds, bar count |
| `GET /analytics/daily-ohlcv` | daily bars for one contract over a date range; `source` selects minute aggregation or vendor daily pass-through |
| `GET /analytics/vwap` | rolling VWAP series, capped at `limit` points and flagged when truncated |
| `GET /quality/report` | the quality report for one contract, frequency and range |
| `GET /quality/missing-timestamps` | every instant with no bar, paged |
| `GET /quality/issue-details` | page through one finding's per-occurrence evidence |
| `POST /insights` | recurring quality patterns and suggested rules for one contract, frequency and range, with the evidence they rest on and anything verification rejected |
| `GET /health` | liveness + version |

Interactive docs at `/docs`.

## Configuration

Environment variables, prefixed `MARKET_DATA_` (see `backend/.env.example`).
All are optional — every value below is already the default. Relative paths
resolve against the working directory, so run the scripts and the API from
`backend/`.

| var | default | meaning |
|---|---|---|
| `MARKET_DATA_DB_PATH` | `backend/data/market_data.duckdb` | DuckDB file |
| `MARKET_DATA_VWAP_WINDOW_MINUTES` | `15` | default VWAP window |
| `MARKET_DATA_SOURCE_MINUTE_TZ` | `America/Chicago` | zone the naive minute wall clock is localised to |
| `MARKET_DATA_DAILY_BAR_TZ` | `America/Chicago` | zone whose calendar date becomes a minute bar's `trading_date`; the dashboard's display zone (`EXCHANGE_TZ` in `frontend/src/app/core/time.ts`) must match it |
| `MARKET_DATA_OUTLIER_MAD_THRESHOLD` | `8.0` | return-outlier sensitivity |
| `MARKET_DATA_VOLUME_SPIKE_THRESHOLD` | `20.0` | volume-spike sensitivity |
| `MARKET_DATA_GAP_MIN_MULTIPLE` | `1.5` | gap threshold, in modal bar intervals |
| `MARKET_DATA_INSIGHTS_MAX_OCCURRENCES` | `50000` | per-finding cap on the occurrences insight statistics are computed from |
| `MARKET_DATA_LLM_MODEL` | `claude-sonnet-5` | Claude model name |
| `MARKET_DATA_LLM_API_KEY` | — | Claude API key, **required for intelligent insights** ([setup](#setting-up-the-claude-api-key)); falls back to `ANTHROPIC_API_KEY` when unset |
| `MARKET_DATA_LLM_MAX_TOKENS` | `16000` | output cap per request |
| `MARKET_DATA_LLM_TIMEOUT_S` | `180` | per request |

### Setting up the Claude API key

Intelligent insights are generated by Claude, through
[LangChain](https://python.langchain.com/), so they need a Claude API key.
Nothing else in the application does. You only have to do this once.

1. **Get a key.** Sign in to the [Claude Console](https://console.anthropic.com/),
   open **API Keys**, and create a key. It starts with `sk-ant-`. Copy it now;
   the console shows it only once.

2. **Create the environment file from the template.** From the repository root:

   ```bash
   cd backend
   cp .env.example .env
   ```

   The file must be `backend/.env`. Settings are read from `.env` in the
   working directory, and the API is started from `backend/`. `.env` is
   git-ignored, so the key is never committed.

3. **Put your key in.** Open `backend/.env` and replace the placeholder on the
   last line with your key:

   ```bash
   # before
   MARKET_DATA_LLM_API_KEY=sk-ant-***
   # after
   MARKET_DATA_LLM_API_KEY=sk-ant-api03-your-real-key
   ```

   Leave the `***` placeholder in and every request is rejected as an invalid
   key. No quotes or spaces around the `=`. In this file the variable must be
   named `MARKET_DATA_LLM_API_KEY`: an `ANTHROPIC_API_KEY` line in `.env` is
   **not** picked up.

4. **Optional: choose the model.** `MARKET_DATA_LLM_MODEL` defaults to
   `claude-sonnet-5`. Change that line only if you want a different Claude model.

5. **Start (or restart) the API** from `backend/`:

   ```bash
   poetry run market-data-serve
   ```

   Settings are read once at startup, so restart the server after any change to
   `.env`. `--reload` watches the code, not `.env`.

6. **Check it works.** Open the dashboard, go to **Data quality**, pick a
   contract and press **Generate insights**. After a short wait, patterns and
   suggested rules appear. If Claude cannot be reached or rejects the key, the
   section says so and why.

**Alternative: a shell variable.** Instead of steps 2–3, export the key in the
terminal that runs the API. It is used only when `MARKET_DATA_LLM_API_KEY` is
unset, so delete that line from `.env` if you have one:

```bash
export ANTHROPIC_API_KEY=sk-ant-api03-your-real-key
poetry run market-data-serve
```

See [docs/insights.md](docs/insights.md#configuring-the-llm) for the remaining
LLM settings.

## Architecture

See [docs/architecture.md](docs/architecture.md), which has a high-level diagram
and one per module. In short: four modules — **data ingestion** (download,
normalise, store), **dashboard presentation** (the two charts and their filters),
**data quality validation** (gaps, missing timestamps, duplicates, values,
outliers) and **intelligent insights** (recurring patterns and the rules they
suggest) — each layered the same ports-and-adapters way, with a **domain**
layer (models, analytics, quality rules, insight evidence and verification)
that is pure — no I/O — and infrastructure plugged in around it (file readers
and the DuckDB repository behind Protocols, the Claude client in `llm/`). Also
[docs/data-ingestion.md](docs/data-ingestion.md),
[docs/data-quality.md](docs/data-quality.md),
[docs/insights.md](docs/insights.md) (how the evidence is computed, what the LLM is asked, and what verification does and does not check) and
[docs/dashboard-charts.md](docs/dashboard-charts.md) (every chart, plus how the
rolling VWAP is computed) and [docs/testing.md](docs/testing.md).

## Acknowledgements

The dashboard charts are drawn with
[TradingView Lightweight Charts™](https://www.tradingview.com/lightweight-charts/),
Copyright © TradingView, Inc., licensed under the Apache License 2.0. The
in-chart TradingView logo is turned off (`attributionLogo: false` in
`frontend/src/app/shared/chart.ts`), so the attribution lives here instead.
