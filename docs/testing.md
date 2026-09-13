# Testing

What is tested, with what, and — more usefully — *which edge cases each layer
was written to survive*. The suites are small enough to read end to end, so this
document is a map rather than a substitute.

Every test in the repo carries a docstring or comment saying **why it exists**,
not what it calls. That is deliberate: a test whose reason is written down
survives a refactor, because the next reader can tell whether the assertion is
still the point.

## At a glance

| | Backend | Frontend |
|---|---|---|
| Runner | [pytest](https://docs.pytest.org) 8 | [Vitest](https://vitest.dev) 4, via the Angular `@angular/build:unit-test` builder |
| Environment | in-memory DuckDB, `tmp_path` | jsdom |
| Property-based | [Hypothesis](https://hypothesis.readthedocs.io) | — |
| HTTP | `fastapi.testclient.TestClient` (real ASGI app) | `HttpTestingController` (`provideHttpClientTesting`) |
| Coverage | `pytest-cov` (branch) | `@vitest/coverage-v8` |
| Static checks | `ruff check` + `ruff format --check`, `mypy src` | `prettier --check` |
| Tests | 157 | 107 |
| Coverage | **97 %** statements/branches | **97 %** statements, **83 %** branches |

Both suites run in under five seconds, which is why they are run on every
change rather than before a commit.

### Commands

```bash
cd backend
poetry run pytest                              # whole suite
poetry run pytest tests/unit                   # units only
poetry run pytest -m integration               # end-to-end only
poetry run pytest --cov=market_data --cov-report=term-missing

cd frontend
npm test                                       # single run
npm run test:coverage
npx ng test                                    # watch mode
```

[CI](../.github/workflows/ci.yml) runs the backend lint, type check and tests on
Python 3.11, 3.12 and 3.13, and the frontend format check, tests and production
build on Node 22.

---

## 1. Strategy

### 1.1 Two tiers, one rule for choosing

| Tier | Location | Rule |
|---|---|---|
| **Unit** | [`backend/tests/unit/`](../backend/tests/unit/) | One function, called directly, on a frame built in the test. A failure names the function that broke. |
| **Integration** | [`backend/tests/integration/`](../backend/tests/integration/) | The real path: a file on disk → normalise → DuckDB → service → FastAPI. Asserts the seams, not the arithmetic. |

The quality rules are called directly with a hand-built `RuleContext` rather
than through `QualityValidator`, so a failure points at the rule rather than at
the validator that ran it.

### 1.2 Three sources of test data, used deliberately

Defined in [`tests/conftest.py`](../backend/tests/conftest.py):

| Source | Used for |
|---|---|
| **Checked-in fixtures** (`clean_minute.csv`, `clean_daily.parquet`, `dirty_minute.csv`) | Anything that has to prove the real pipeline works end to end. |
| **Synthetic generators** ([`synthetic.py`](../backend/src/market_data/synthetic.py)) | Data with a *specific* defect punched into it — a hole, a duplicate, a bad price. |
| **Hand-built canonical frames** | Analytics arithmetic, where the numbers have to be readable off the page. |

The generators are themselves under test
([`test_synthetic_and_gaps.py`](../backend/tests/unit/test_synthetic_and_gaps.py)):
`clean_*` really obeys the OHLC invariants and emits business days only, and
`dirty_*` really carries exactly the three defects downstream tests look for.
Without that, a silent drift in a generator would weaken every test built on it
while failing none of them.

### 1.3 Isolation

Every repository is `:memory:` or under `tmp_path`, so tests never touch a
developer's store and run in any order. The `api_client` fixture clears both
process-wide caches (`get_settings`, the repo singleton) **on the way in and on
the way out**, because a leftover one would point the next test at the previous
test's database.

### 1.4 Oracles over re-implementation

Where a value is independently checkable, the test recomputes it the slow,
obvious way in plain Python and compares — rather than re-expressing the same
Polars idiom the implementation uses. A test written in the implementation's own
vocabulary passes for the same reason the code is wrong. This is how rolling
VWAP is checked (§2.2).

### 1.5 The negative tests

Every "this rule fires" test is paired with a "this rule stays quiet" test:

- the clean minute fixture produces **zero errors** through the whole validator;
- the validator on an empty frame produces **no issues**, not "nothing good";
- the outlier check stays silent below its minimum sample and on a zero-spread
  distribution.

These are what make a finding elsewhere worth trusting.

---

## 2. Backend — analytics

### 2.1 Daily OHLCV bars

[`tests/unit/test_analytics.py`](../backend/tests/unit/test_analytics.py) ·
[`daily_bars.py`](../backend/src/market_data/domain/analytics/daily_bars.py)

The happy path asserts that each field aggregates *its own way* — first, last,
max, min, sum, count. The fixture's session high (10.5) and low (9.6) sit
**mid-run on purpose**, so a `high` accidentally taken from the first or last bar
fails here instead of coincidentally agreeing.

| Edge case | Why it is a trap | Expected behaviour |
|---|---|---|
| **Several (contract, session) groups at once** | An `open`/`close` taken globally rather than per group | Contract B's Tuesday must not open at contract A's Monday price; output is sorted |
| **Unordered input** | Nothing guarantees a frame arrives sorted — concatenated sources, a re-ingested correction | First and last are decided by `ts`, never by row position |
| **A session crossing midnight UTC** | A futures session runs through midnight; grouping on the UTC *date* would split it in two and truncate both halves | Grouped on `trading_date`: one bar, opening at 23:00 and closing at 01:00 the next UTC day |
| **Null volume on some bars** | A null propagating through `sum` would null the session total | Skipped; the session totals the bars that have one |
| **Null volume on *every* bar** | — | Reports `0` |
| **Open interest** | Summing it would multiply the position count by the bar count | Carried forward as a *level*: the last value actually **reported**, so a trailing null does not erase it |
| **Null price at either end** | Cannot arrive from a file (ingestion rejects it), but the function is also called on hand-assembled frames | Falls through to the nearest bar that has a price |
| **Already-daily input** | The same endpoint serves both frequencies | Collapses against itself: one bar per session, unchanged, `bar_count == 1` |
| **Empty frame** | — | Zero rows, not one row of nulls |

### 2.2 Rolling 15-minute VWAP

[`tests/unit/test_analytics.py`](../backend/tests/unit/test_analytics.py) ·
[`vwap.py`](../backend/src/market_data/domain/analytics/vwap.py)

The central test is an **oracle**: VWAP is recomputed from its definition in
plain Python — typical price `(high + low + close) / 3`, volume-weighted over a
trailing window **open at the left and closed at the right** — and compared bar
by bar. That phrasing is the assertion: a window that includes its own left edge,
or is off by one bar, moves the number.

| Edge case | Why it is a trap | Expected behaviour |
|---|---|---|
| **Partial window at the start of the series** | A minimum-sample rule would silently drop the first bars | No minimum: whatever bars fall in the span. The oracle applies the same rule, so this is pinned rather than assumed |
| **Zero-volume window** | A volume-weighted average of nothing | `null` — never `0`, which would plot on the chart as a real print at price zero, and never a `ZeroDivisionError` |
| **A zero-volume bar inside a traded window** | The guard is on the *window* sum, not the bar | Still gets a real VWAP |
| **Gap shorter than the window** | A forward fill or interpolated bar would move the average | Fewer bars in the windows that cross it. Nothing is emitted for an absent instant, nothing is carried forward |
| **Gap longer than the window** | The first bar after a session break | Its VWAP is its own typical price; nothing from before the break is in range |
| **Two bars on one instant (conflicting duplicates)** | Neither variant is more correct — that is what makes them a conflict | Both are weighted into the sum. The instant carries **one** VWAP shared by both rows, while each row keeps **its own** typical price |
| **Empty frame** | The result is concatenated and serialised downstream | Empty, but with the columns `contract, ts, typical_price, vwap` intact |

The duplicate case guards a real regression, recorded in the test: a join on
`(contract, ts)` to recover each bar's typical price used to **fan out** — 6 bars
became 8 rows, half of them pairing one variant's typical price with the other's
VWAP.

**Property test (Hypothesis, 40 examples):** a weighted average of prices can
never leave the range of the prices that produced it. Hypothesis generates 3–25
bars with arbitrary prices and positive volumes; the assertion is
`low.min() ≤ vwap ≤ high.max()` within a float epsilon. This is the invariant
that catches a mis-weighted sum or a window joined to the wrong rows — both of
which push the result outside its own inputs — over shapes no hand-written
fixture would cover. Volumes are constrained positive deliberately: negative
volume survives ingestion, and the property describes what the maths does with
*sound* input, not garbage.

---

## 3. Backend — normalisation (data cleaning)

[`tests/unit/test_normalize.py`](../backend/tests/unit/test_normalize.py) ·
[`normalize.py`](../backend/src/market_data/ingestion/normalize.py) · 19 tests

Each test introduces **exactly one** defect into a well-formed row, so the
outcome can be attributed to it. What is asserted is the bucket a row lands in —
`clean`, `rejected`, `superseded` — and the counters that must make the
arithmetic add up (`rows_read`, `rows_deduplicated`, `rows_superseded`).

### 3.1 Frequency detection

Getting this wrong mis-stamps **every bar in the file** rather than failing
loudly, because frequency decides how the timestamp is read. Tests pin the whole
priority chain: explicit override → path hint → column signature → minute.

- A path hint (`data/daily/...`) outranks anything in the frame.
- With no path, the marker column identifies the file: only daily bars carry
  `open_interest`.
- With neither, it **defaults to minute** — guessing daily would silently
  mis-stamp a minute file.

### 3.2 Timestamp semantics — the trap the module exists for

The dataset splits timestamp meaning by frequency, and the two must never be
confused:

| File | Raw value | Handling | Test |
|---|---|---|---|
| Minute | tz-**naive** Chicago wall clock | Localised to `America/Chicago`, then converted | `09:00` in June (CDT) → `14:00Z`; the same wall clock in January (CST) → `15:00Z` |
| Daily | epoch ms, already a UTC instant | **Stamped** UTC, never shifted | `1704067200000` → `2024-01-01T00:00Z`, `trading_date` = that UTC date |

The offset is read from the tz database for that date, never hard-coded, which
is exactly what the January/June pair proves.

**DST spring-forward** gets its own reject reason (`ambiguous_or_nonexistent_local_time`):
`2024-03-10 02:30` America/Chicago does not exist. It must *not* collapse into
`unparseable_timestamp` — the value parsed fine, it just names an instant that
never happened, which points at the vendor's export rather than at a corrupt
field.

### 3.3 Rejected vs flagged — where the line sits

The line is **structural**: a row that cannot form a bar is rejected at
ingestion; a row that forms an *implausible* bar is kept and left to the quality
layer, where a human can see it and decide. These tests exist to catch that line
moving.

| Input | Outcome |
|---|---|
| No contract symbol | Rejected, `missing_contract` |
| Price that will not parse (`"not-a-number"`) | Rejected, `unparseable_price` |
| No usable timestamp column | Rejected, `unparseable_timestamp` — and `rows_read` still counts it, because a rejected row is *reported*, never lost |
| No OHLC columns at all | Rejected, `missing_ohlc`, after the absent columns are materialised as null so the per-row expressions have something to look at |
| **`high < low`** | **Kept.** An impossible bar is still a bar; the quality layer reports it |

Two defects are put in one frame on purpose, so a single catch-all reason would
fail the test.

### 3.4 One bar per instant: deduplicate vs supersede

The read side assumes one bar per `(contract, ts)`, so the conflict is settled at
ingestion. The two ways a row can disappear are kept deliberately apart:

- **Byte-identical rows** → collapsed, `rows_deduplicated` incremented,
  **nothing** written to the audit trail. An exact duplicate loses no
  information, so it must not pollute the record of things a human might need to
  explain.
- **Same instant, different values** → last row in file order wins,
  `rows_superseded` incremented, and the loser is kept **whole** with a pointer
  (`winning_row_hash`) at what replaced it, so the tie-break can be audited
  against the stored bar.

Ingestion order therefore decides the tie-break, which is why `expand_paths` is
tested to return a **sorted** list (§5.1).

### 3.5 Vendor file shapes

The shipped dataset is one vendor's layout; these cover the fallbacks kept for
files that name the same things differently.

- **Column aliases:** `ticker → contract`, `px_open/px_high/px_low → o/h/l`,
  `qty → volume`, and the one that matters — **`settle → close`**, because a
  daily settlement *is* the close and dropping it leaves the bar with none.
- **Timestamp fallback chain:** `timestamp_chicago_wall` → `timestamp` →
  `datetime`, each still localised for minute files and still stamped for daily.
- **A file that is not bar data at all** (no OHLC, no timestamp): every row
  rejected under one reason, `missing_required_columns`. The reject report keeps its
  fixed shape `[source_file, row_number, reason]` — a regression test, because
  it used to carry the vendor's own columns through, which made *persisting* the
  rejections fail on the file that needed reporting most.

---

## 4. Backend — data quality

[`tests/unit/test_quality.py`](../backend/tests/unit/test_quality.py),
[`test_issue_details.py`](../backend/tests/unit/test_issue_details.py),
[`test_missing_timestamps.py`](../backend/tests/unit/test_missing_timestamps.py)

What is asserted is the issue **code** and its **severity** — severity is what
the dashboard sorts and colours by, and several rules fire at a different level
depending on frequency, which is the part most easily got wrong.

### 4.1 Severity depends on frequency

| Case | Minute | Daily | Reason |
|---|---|---|---|
| `high < low` | **error** | **error** | No convention can excuse it at any frequency |
| `close` outside `[low, high]` | **error** | **info** | Nothing settles intraday, so a minute bar is broken. A daily settlement is set by the exchange and legitimately lands outside the traded range |

Those two `close_out_of_range` tests are a **pair** — neither is meaningful
alone. Together they pin that severity is read from the frequency rather than
hard-coded.

### 4.2 Two bars on one instant mean two different things

| Case | Code | Severity |
|---|---|---|
| Same instant, **different** prices | `conflicting_duplicate` | **error** — one of them is wrong and nothing in the data says which, so a human must decide |
| Same instant, **identical** bar | `repeated_timestamp` | **warning** — it agrees with itself; a sloppy export, not a doubt |

The second test asserts the **exact** issue list, not a membership check,
because the failure mode is raising a conflict alongside the repeat.

### 4.3 The statistical guards

The outlier check is median/MAD based, so it has two ways to become meaningless.
Both are tested to stay **silent**, and a third test proves it still accuses when
it has a real baseline:

| Input | Expected |
|---|---|
| 29 bars with wild moves | Nothing. Under 30 observations, median/MAD describes nothing |
| 59 flat bars + one jump | Nothing. A locked market gives MAD = 0; dividing by it would flag every move |
| 60-bar random walk with one 3× jump | `price_return_outlier` fires |

### 4.4 Calendar and gaps

- **Missing sessions:** one business day is dropped mid-series from the real
  ingested daily fixture; `missing_sessions` must notice.
- **Intra-session gap:** a 30-minute hole punched into generated data, taken all
  the way through *generator → normalise → validator*, must come out as
  `intra_session_gap`. This is the test that proves all three layers agree on the
  time axis.
- **Expected breaks are never listed.** A weekend is not missing data, and
  listing it would bury the real holes.

### 4.5 The missing-timestamp listing

Rather than hard-coding the ten absent instants, the test states the two
properties that must hold **for any hole**, so it survives a change to the
generator's start time:

1. every instant reported absent really is absent (disjoint from the bars that
   exist), and
2. the reported run is contiguous on the inferred grid — the listing has no
   holes of its own.

The bar interval is **inferred from the data** (asserted as 60 s), not assumed:
nothing in the file declares it, and getting it wrong would invent or hide gaps.
An empty frame returns an empty page, not an error, because the dashboard asks
for this before anything has been ingested.

### 4.6 Evidence, caps and paging

A summary ("9 594 intra-session gaps") says something is wrong; the details say
*where*. The invariant across every test here is that **a truncated list still
reports the true total** — a trimmed list with a trimmed total would silently
understate exactly the problem the truncation flag exists to signal.

- Under the cap: nothing trimmed, `detail_truncated` false.
- Over the cap: list trimmed to the **first** rows (so callers must pass them in
  reading order), `detail_total` unchanged, flag set.
- Caller pre-sliced the frame and declared the real total (the path the rules
  take): the declared total wins.
- A caller's `total` that is **smaller** than the rows shipped is ignored — it
  must never make the list look longer than it is.
- `date_detail` labels by date *and* stamps UTC midnight, because the label is
  what a user reads and the instant is what positions the marker on the chart.
- Every wired rule is asserted to report a `detail_total` matching its own
  evidence.
- Paging arithmetic (`has_more`) is parametrised over the two cases that strand a
  client in a loop: a final page landing **exactly** on `total`, and a short page
  stopping before it. Both must report `False`.
- A severity tally counts **occurrences, not issues** — one issue can stand for
  9 594 gaps, and a per-issue tally would read as 1. A clean report tallies `{}`,
  not zeros, because the dashboard renders on presence.

### 4.7 Intelligent insights

[`test_insights_evidence.py`](../backend/tests/unit/test_insights_evidence.py),
[`test_insights_verify.py`](../backend/tests/unit/test_insights_verify.py),
[`test_insights_service.py`](../backend/tests/unit/test_insights_service.py),
[`test_llm.py`](../backend/tests/unit/test_llm.py)

An insight is only as trustworthy as the evidence under it, so most of these
tests are about the evidence and about how the pipeline fails, rather than
about wording.

**Evidence.** Reports are hand-built so each test names the occurrences it is about.

| Edge case | Why it is a trap | Expected |
|---|---|---|
| Hours | 21:00 UTC is the 16:00 CT halt; counted in UTC it looks like nothing in particular | Counted in exchange time |
| A late-evening occurrence | 03:00 UTC on Tuesday is Monday evening in Chicago | Filed under Monday, hour 22 |
| Daily findings | A daily `ts` is a date at UTC midnight; shifted into Chicago it lands on the previous day | Kept on its own date, with no hours, and no share of sessions for a *missing* session |
| "After a gap" | A gap detail's `end_ts` is the last *missing* bar | Measured from the bar that resumed the series, one interval later |
| A gap finding that does not state its interval | Only `intra_session_gap` carries it | Inferred from the detail's own length and missing bars |
| A capped finding | A sample must not pass for the whole | `analysed` reports the rows read; `per_1k_bars` uses the real count |

**Verification** checks the schema and nothing more, and the tests pin exactly
that: a complete pattern or suggestion passes untouched, its params exactly as
sent; an enum value that does not exist, a bad `type` or `kind`, or a null
rationale is refused with the offending field named in the reason; and one
malformed item costs only itself, so the good item beside it survives. Nothing
checks a figure, a citation or a parameter against the evidence, so no test
claims to.

**Service.** `InsightsService` is driven by a fake client: an answer without the
expected `patterns` or `suggestions` list is an `InsightProviderError` rather
than zero patterns, and an empty pack makes no pattern call, just as no
patterns makes no suggestion call.

**LLM adapter.** No model is called: `ClaudeClient` takes its LangChain chat
model as a parameter, so a scripted one stands in and replays a
`with_structured_output(..., include_raw=True)`-shaped answer (`raw`, `parsed`,
`parsing_error`) or raises the Anthropic SDK exception LangChain would.
Structured output is requested with `method="json_schema"`, Claude's own
schema-constrained format, so no code-fence stripping or a `json_object`
fallback is needed. Every way of *not* getting an answer — a refusal, an
answer cut off at `max_tokens`, a parsing failure or no parsed output, a
404/429/401, an unreachable endpoint — is an `InsightProviderError`, which the route reports
as a 503 with the reason. The schemas are asserted to offer exactly the
domain's enums, so the prompt cannot drift from the models, and to carry the
title LangChain needs before it will send a raw JSON Schema.

---

## 5. Backend — integration

### 5.1 Readers and path expansion

[`test_readers.py`](../backend/tests/unit/test_readers.py) — dispatch is on
extension alone, so the two failures possible are a case mismatch (`.CSV` must
read as CSV) and an unsupported format, where **the message is the whole value of
the error** and must name the formats that *are* supported.

Provenance is recorded here or nowhere: every raw row carries its resolved
absolute source path (so the same file reached by two relative paths reports one
origin) and a 1-based row number excluding the header, so a rejection can say
"file X, row N" and match what a user sees in a spreadsheet.

`expand_paths` collapses files, directories and globs into one sorted,
duplicate-free list: directories are walked **recursively**, a directory plus a
file inside it does not ingest that file twice, a quoted glob is expanded by us
rather than the shell, and a non-existent path returns `[]` rather than raising
(the CLI turns the empty result into its own error). Sorted matters because
ingestion order decides the last-write-wins tie-break in §3.4.

### 5.2 Pipeline and services

[`test_pipeline_and_services.py`](../backend/tests/integration/test_pipeline_and_services.py)

| Test | What it protects |
|---|---|
| **Idempotent ingestion** | Loading the same file twice leaves the store exactly as it was. Re-ingesting is a normal operation |
| **A later run corrects an earlier bar** | The same instant arriving across two runs — a real vendor correction — overwrites the bar and files the old row away |
| **Resolved conflicts stay visible** | A conflict settled at ingest is still reported by the quality layer, and re-ingesting does not inflate the count |
| **Derived daily matches its minute bars** | The daily bar is checked against the source bars rather than a hard-coded number |
| **VWAP series caps and declares it** | A client must be able to tell a capped series from a short one |
| **A non-bar file is reported, not raised** | Pointing the ingester at the wrong file produces a rejection report |
| **`ingest_frame`** | The in-memory seam tests and notebooks use instead of a file |

### 5.3 HTTP surface

[`test_api.py`](../backend/tests/integration/test_api.py),
[`test_issue_details_api.py`](../backend/tests/integration/test_issue_details_api.py)
— real FastAPI app, real store. What is tested is the **wire contract**: status
codes and the JSON shape a client parses.

- `/health` answers **without a store**, since a container probe hits it first.
- The contract listing exposes a date range, because the UI seeds its pickers
  from it.
- A missing required parameter is a **422 from validation**, not a 500 from a
  query that ran unfiltered. A negative offset is rejected on **both** paged
  routes, tested separately because they are separate routes rather than shared
  code.
- An unknown issue code is an **empty page, not a 404** — the dashboard asks for
  details for whatever code it is showing.
- Paging is asserted to have **no overlap and no skips** against the head of the
  whole list, the total **survives** paging (a total recomputed per page would
  shrink as the offset advanced), and the endpoint **reaches past** the sample
  the report itself carries. That last one is *asserted*, not skipped: a fixture
  that fell under the cap would leave the test passing while proving nothing.
- The tests use their own `holed_api` fixture, because the checked-in fixtures
  are gap-free and a listing test against them would pass while asserting
  nothing.

[`test_insights.py`](../backend/tests/integration/test_insights.py) plants one
unmistakable regularity — the 16:00–17:00 CT halt missing from five sessions —
and takes it through bars → validator → evidence → a scripted provider →
verification → route. No model is called. The evidence the model is shown must
reveal the halt (hour 16, every session, a 61-minute median) and must read every
occurrence even with the dashboard's detail cap set to 2. A scripted model that
is right about one pattern and invents a trend in another has **both** kept,
because verification checks shape rather than grounding, and both are what the
suggestion call is shown. Of its suggestions, the two well-formed ones survive,
including a threshold change the pattern says nothing about, and one whose
`type` does not exist is reported in `rejected` with its reason. A model that cannot answer raises rather than returning an empty report,
and on the wire that is a **503 that says why**, not a 500. `POST` only (a `GET`
is 405, because generating must never be a side effect of a read), a contract is
required, and an empty range returns no patterns without calling the model.

### 5.4 The CLI

[`test_ingest_script.py`](../backend/tests/integration/test_ingest_script.py) —
the script is the only way a user loads data, so its contract is the **printed
summary and the exit code**, not just what lands in the database. A dirty file is
a normal outcome (exit 0, counts on stdout); a path matching nothing is a user
error (non-zero, message on stderr); an explicit `--frequency` overrides
inference, which is the only escape hatch when a vendor file's name and columns
do not identify it.

---

## 6. Frontend

Specs sit beside the code they cover. Components are rendered through `TestBed`
against a **fake store** or a **stubbed child**, so each spec asserts one layer.

| Spec | Layer under test |
|---|---|
| [`format.spec.ts`](../frontend/src/app/core/format.spec.ts) | Pure formatters |
| [`time.spec.ts`](../frontend/src/app/core/time.spec.ts) | Chicago-time display and the chart time shift |
| [`api.spec.ts`](../frontend/src/app/core/api.spec.ts) | The wire contract with FastAPI |
| [`store.spec.ts`](../frontend/src/app/core/store.spec.ts) | Filter selection → the queries issued |
| [`app.spec.ts`](../frontend/src/app/app.spec.ts) | Shell: filters, tabs, empty states, the time-zone note |
| [`analytics.spec.ts`](../frontend/src/app/analytics/analytics.spec.ts) | Headline figures and **chart series** |
| [`quality.spec.ts`](../frontend/src/app/quality/quality.spec.ts) | Findings table, evidence drill-down, missing listing |
| [`insights.spec.ts`](../frontend/src/app/quality/insights.spec.ts) | Insights section: on request only, evidence figures, failure reasons and rejections |
| [`stat.spec.ts`](../frontend/src/app/shared/stat.spec.ts) | The dumb stat tile |

### 6.1 Charts — how they are tested without a canvas

jsdom has no canvas, so `lightweight-charts` cannot draw. Rather than mock the
library, the panel's `Chart` component is **replaced with a stub** via
`TestBed.overrideComponent`, and the stub records the `series` input it was
given. That leaves exactly what is worth asserting — **the data handed to the
chart**, which is where the null handling, the keying and the unit conversion
live — and drops what is not (that a third-party library draws pixels).

[`chart.ts`](../frontend/src/app/shared/chart.ts) is therefore **excluded from
coverage** in [`angular.json`](../frontend/angular.json), deliberately and
visibly: it is imperative canvas lifecycle code with no branches worth asserting
in jsdom.

**Candlestick and volume series**

| Edge case | Expected |
|---|---|
| A bar missing **any** of its four prices | Dropped. `lightweight-charts` cannot draw a partial candle, and a zero-filled one would be a lie about the session |
| Keying | By **`trading_date`**, the Chicago calendar date, not by timestamp, so the axis needs no time-zone conversion |
| Volume | Drawn as a `volume` series on the same chart, keyed by the same `trading_date`, so a bar sits under its candle |
| A session dropped for a missing price | Keeps its volume bar: missing prices do not erase what traded |
| A session with a null volume | No bar, rather than a bar at zero |
| Volume colour | Red when the session closed below its open, green otherwise |

**Rolling VWAP chart**

| Edge case | Why it is a trap | Expected |
|---|---|---|
| Daily frequency selected | A rolling intraday window is undefined on daily bars | Renders "needs minute bars" instead of an empty chart, and **no** chart is put into intraday mode |
| Time axis | The library takes a **date string** for candles but **seconds** for an intraday line, and labels every time as UTC — the easiest conversion to get wrong | Chicago wall-clock seconds: `2024-06-03T14:00:00Z` is plotted at `09:00` (CDT, UTC-5) |
| Chicago falls back from daylight saving | The wall-clock hour 01:00–02:00 happens twice, and the chart throws on a time that does not increase | The second pass is dropped, so only strictly increasing times reach the chart |
| A point whose VWAP is `null` (zero-volume window, §2.2) | The typical price still exists | The VWAP line drops that point; the typical-price line keeps **both** |
| The window heading | The heading is the only place the window appears | Names the window the user actually chose (60, not a stale 15) |

**Headline figures**

| Edge case | Expected |
|---|---|
| Range change | Measured **close to close**. A test bar carries a 500 high as bait: a change taken from extremes would report a move the contract never closed at |
| Direction | Asserted in **both** directions, so the sign is proven rather than inferred from the gain alone |
| A single session | `—`. One close is a level, not a change; reporting 0 % would read as "flat", a claim the data does not support |
| Opening close of zero | `—`, not `Infinity%` |
| Volume with a null session | Summed with null treated as zero **for the sum only** — a multi-session total is still worth showing |

**The session table**

No bars renders a placeholder rather than a header row with nothing under it,
which reads as a load that failed halfway. A bar with holes renders `—` per
cell, asserted cell by cell, because the table is where a zero-filled bar would
be most convincing and most wrong.

### 6.2 Formatters

The single rule under test: **absent is a dash, never a zero** — "no trade" and
"traded at zero" are different claims about a bar. Beyond that: four decimals as
a *cap* not a pad (`78.1` stays `78.1`), thousands grouped so five-figure index
futures stay scannable, volume rounded to whole units, percentages explicitly
signed with zero on the non-negative side so a column keeps one shape.

Timestamps are formatted by [`time.ts`](../frontend/src/app/core/time.ts) and
tested in `time.spec.ts`: rendered in **Chicago time regardless of browser
locale**, including the empty string, which the API sends for an unknown instant
and which `new Date('')` would otherwise format as `Invalid Date`. The
daylight-saving cases are asserted explicitly: `15:30Z` is `09:30` in January and
`10:30` in July; a Chicago evening such as `03:00Z` falls on the **previous**
date; midnight renders as `00:00`, not `24:00`; and the per-hour offset cache
switches at exactly `08:00Z` on spring-forward day, not a minute early.

### 6.3 The API client

These assert the **exact paths and query-parameter names** the backend routes
declare, because a rename on either side is otherwise invisible until the
dashboard silently shows nothing. The translations that earn their own test:
`windowMinutes → window_minutes`, and the daily-OHLCV route taking the selected
frequency as **`source`**, because the parameter names the *input* bars rather
than the output.

Two behavioural cases: an `undefined` query must mean "do not ask", not "ask for
everything" (`expectNone`), and moving a filter must **refetch on its own**,
since the resource tracks whichever signals the query function reads and no
subscription is written anywhere.

Insights are the one `POST`. The request carries the selection but **not** the
run number, which exists only to make the resource ask again, and a new run is
asserted to post again with unchanged filters — otherwise Regenerate would do
nothing.

### 6.4 The store

Tested through a `FakeApi` that **records each query function** and evaluates it
on demand, so the store's intent is read with no HTTP in the picture.

- **Opening state:** asks for nothing until a contract and a range exist — "ask
  for nothing", not "ask with blanks".
- **Defaults:** last 30 days for minute data (so the first view stays fast), a
  year for daily (where 30 days would be too few bars), never starting before the
  data does, and re-opened when the contract changes so the range points at real
  data.
- **Derived queries:** no VWAP requested on daily data; evidence requested only
  once a finding is open.
- **Paging:** steps a page at a time, never pages before the start of the list,
  toggling an open finding closes it, switching findings starts the new one at
  its **first** page, and a new filter **rewinds both pagers** and closes the
  open finding.
- **Insights on request:** no insights query until the button is pressed, none
  before a selection exists, a new run on every press, and a filter change drops
  the request **at once** — before any effect runs, because the resource may read
  the query first — so returning to the old selection does not quietly generate
  again.

### 6.5 Shell and quality panel

The empty states matter more than they look: they are the first thing a reviewer
sees if the API is down or nothing has been ingested, so each is asserted to say
**what to run** — how to start the API, how to ingest — rather than merely that
something is missing. "Still loading" must not be rendered as "the store is
empty". The filters are hidden entirely while there is nothing to filter, the
date inputs are asserted not to cross each other, the VWAP window is disabled on
daily data, and the window is written back to the store as a **number, not
text**. The note under the title stating that every date and time is Chicago
time is asserted to render in every state, including the empty ones.

In the quality panel: tallies count occurrences rather than findings (§4.6); a
clean report reads as **clean**, not as an empty table; a failed load says so.
The drill-down paints the report's own sample first, takes the fetched page over
it once that lands, and **ignores a page belonging to a finding that is no longer
open** — the race a user creates by collapsing one finding while its request is
in flight. Minute absences are grouped under their **Chicago** date in
first-seen order (so the headings follow the data rather than a re-sort, and
`03:00Z` lands under the previous day, the date the filter and the daily candle
give it), daily absences are
listed flat because grouping dates by date would put one row under every heading,
and a range with nothing missing reads as clean rather than empty. Finding
timestamps print in Chicago time on minute data, and as the bare, unshifted date
on daily data.

The insights section shows a button and nothing else until it is pressed (and
the quality page is asserted not to press it), disables the button while a run
is in flight so runs cannot stack, and renders each pattern's figures **from its
evidence rows** rather than from the explanation — the explanation in the test
deliberately contains no figures. A custom rule reads as a sentence rather than
a parameter table, output is labelled AI-assisted with the model's name, a
failed run passes on the API's reason (an unreachable model, say), and whatever
verification removed is listed with its reasons.

---

## 7. Coverage

**Backend — 97 %** statements and branches. The uncovered remainder is
concentrated in [`missing.py`](../backend/src/market_data/domain/quality/missing.py)
(74 %), [`duckdb_repo.py`](../backend/src/market_data/storage/duckdb_repo.py)
(96 %), [`gaps.py`](../backend/src/market_data/domain/quality/checks/gaps.py)
(90 %) and [`verify.py`](../backend/src/market_data/domain/insights/verify.py)
(93 %) — mostly defensive branches on shapes the pipeline upstream of them
cannot produce, and in `verify.py` the fallbacks for labelling a draft that
has no usable name.

**Frontend — 97 %** statements, 98 % lines, **83 %** branches. Branch coverage
is the lower figure because template null-guards have two arms and the specs
exercise the one that matters. Coverage excludes `chart.ts` (§6.1) and
`app.config.ts` (framework wiring).
