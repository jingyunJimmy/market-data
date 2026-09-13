# Data Quality Handling

How the pipeline deals with missing values, duplicate records, malformed bars,
data gaps and statistical outliers — where each is caught, the logic and formula
behind it, what happens to the row, and how it reaches the dashboard.

Each detector below is laid out the same way: **the logic** (how the check is
computed, and why it is computed that way rather than the obvious alternative)
followed by **in the UI** (which table it lands in, what its Occurrences column
counts, and which evidence columns its drill-down shows). The rendering
mechanics those sections refer to are described once, under
[The UI tables](#the-ui-tables).

## Two layers

| Layer | Where | What it does | Effect on the row |
|---|---|---|---|
| **Ingestion / normalise** | [`ingestion/normalize.py`](../backend/src/market_data/ingestion/normalize.py) | Hard gate. Rejects rows that cannot structurally form a bar. | Row never enters the `bars` table; a pointer to it is written to `rejected_rows`. |
| **Quality** | [`domain/quality/`](../backend/src/market_data/domain/quality/) | Soft report. Inspects rows that *are* in `bars` and raises typed issues. | Row stays in `bars`; it is only flagged. Nothing is deleted. |

Analytics (`daily_ohlcv`, `rolling_vwap`) read exclusively from `bars`, so a
rejected row is automatically excluded from every aggregation; a *flagged* row is
not.

---

## 1. Missing values — any OHLC field null

**Caught in:** `normalize`, Step 8 (structural rejection).

```python
.when(pl.all_horizontal(pl.col(c).is_null() for c in OHLC_COLUMNS)).then("missing_ohlc")
.when(pl.any_horizontal(pl.col(c).is_null() for c in OHLC_COLUMNS)).then("missing_ohlc")
```

Any null in `open/high/low/close` → the row is tagged `missing_ohlc`, kept out of
`bars`, and written to `rejected_rows`.

| Requirement | Implementation | Status |
|---|---|---|
| Mark as invalid | `rejected_rows(run_id, source_file, row_number, reason)` + per-reason tally in the run summary + printed by the CLI | ✅ |
| Exclude from aggregation | Not in `bars`; analytics only read `bars` | ✅ |
| Keep in a raw table for audit | ⚠️ Only a **pointer** is kept (source file + row number + reason). The row's original OHLCV values are not stored in any queryable table — audit relies on the immutable source file plus `row_number`. | ⚠️ partial |

Related distinctions:

- A price field that is a **non-null string which fails to parse** (e.g. `"12.5x"`)
  → reason `unparseable_price`, not `missing_ohlc`. It is checked *first*, so an
  unparseable price never hides behind a `missing_ohlc` tag.
- A **minute bar with null `volume`** (not an OHLC field) is **not** rejected. It
  stays in `bars` and is flagged `missing_volume` (WARNING) by the quality layer;
  VWAP treats null volume as 0.

---

## 2. Duplicate records

Two distinct cases, handled very differently. Both are resolved **at ingest**,
so `bars` holds exactly one row per `(contract, frequency, ts)` and everything
downstream can rely on that.

### The logic: one identity, two keys

Both kinds of duplicate are decided by comparing keys, never by a fuzzy match.
There are exactly two keys, and which one a pair of rows collides on is what
tells the two cases apart.

```
row_hash = sha1( contract | frequency | ts | open | high | low | close | volume )
instant  = (contract, frequency, ts)
```

`row_hash` is built in `_row_hash_expr` from the *business* fields only —
`source_file`, `ingested_at` and the row number are excluded on purpose, so the
same bar re-read from a different file hashes the same. Prices are rounded to
10 decimals before stringifying so float representation noise cannot split one
bar into two hashes, every part is null-filled to `""` so a missing volume
cannot null the whole hash, and SHA-1 is used rather than Polars' `.hash()`
because the value is persisted and must be stable across processes and library
versions.

`frequency` sits in both keys. A minute bar and a daily bar for the same
contract can share a midnight timestamp, and without it the daily bar would
silently overwrite the minute bar.

| The two rows agree on | and on | verdict |
|---|---|---|
| `instant` | `row_hash` | **exact duplicate** — a copy, one is kept, nothing is lost |
| `instant` | — | **conflicting duplicate** — the source disagrees with itself |
| — | `row_hash` | impossible: `row_hash` contains the instant |

Counting follows the same split:

```
exact:       count = copies - 1                       per repeated row_hash
conflicting: count = |{ distinct ts in superseded_bars }|   per contract
```

The conflict count is by **distinct instant**, not by row, so re-ingesting the
same file does not inflate it. The raw audit-row count is carried separately as
`context.discarded_rows`, and that one does grow with each run.

### 2a. Exact duplicates — every business field identical (same `row_hash`)

**Caught in:** `normalize` + storage.

- **Within one file:** `clean.unique(subset=["row_hash"], keep="first")` — counted
  as `rows_deduplicated`.
- **Keep rule:** `keep="first"` (first occurrence in file order).
- Nothing is lost when byte-identical rows collapse, so they are only counted,
  never written to the audit trail.
- Quality rule `exact_duplicates` (ERROR) now only fires on a raw frame
  validated *before* persistence; stored data cannot contain them.

### 2b. Conflicting duplicates — same `(contract, frequency, ts)`, different OHLCV

**Resolved in:** ingestion, by a **last-write-wins** rule. **Reported by:** the
quality layer, from the audit trail rather than from `bars`.

This is the case where the source disagrees with itself: the same instant
carrying two different bars. It is resolved rather than reported-and-kept,
because one bar per instant is the invariant every read path depends on.

**The rule.** The latest row wins. "Latest" has two scopes:

| scope | mechanism | audit reason |
|---|---|---|
| within one file | `_resolve_instant_conflicts` in [`normalize.py`](../backend/src/market_data/ingestion/normalize.py) dedupes on the key with `keep="last"`, ordered by `__row_number` so the choice does not depend on Polars' group ordering | `superseded_in_file` |
| across files / runs | the composite primary key plus `INSERT … ON CONFLICT (contract, frequency, ts) DO UPDATE` in [`duckdb_repo.py`](../backend/src/market_data/storage/duckdb_repo.py) | `superseded_by_run` |

**Nothing is discarded silently.** Every displaced row is written to
`superseded_bars` **in full** — all OHLCV columns, its provenance, and the
`winning_row_hash` of the row that replaced it. So any stored bar can be traced
back to what it superseded, and the decision is reversible.

`superseded_bars` is deliberately **not** `rejected_rows`. A rejected row is
malformed and never entered `bars`; a superseded row was perfectly valid and
lost a tie-break. Merging them would make the reject tally read as a
data-quality figure when it is really a conflict-resolution figure, which is
also why the CLI prints them as separate counts:

```
dirty_minute.csv: +99 bars, 3 rejected {...}, 4 superseded

Total: 99 ingested, 3 rejected, 4 superseded (see superseded_bars)
```

**Resolved does not mean hidden.** Because the conflicts are gone from `bars`,
the old `conflicting_duplicate` rule would now find nothing. The quality report
reads `superseded_bars` instead, via a new rule `resolved_instant_conflict`
(WARNING):

```
[warning] conflict/resolved_instant_conflict (x4) 4 timestamps had conflicting
          OHLCV values for CL_TEST; the latest row was kept and the rest are in
          superseded_bars
```

WARNING rather than ERROR: the store is now self-consistent and the full audit
trail exists, so this is a fact about the feed, not a defect in the data you are
querying. It is counted by distinct instant, per the formula above.

| Requirement | Implementation | Status |
|---|---|---|
| Count in the quality report | `resolved_instant_conflict.count`, by distinct instant | ✅ |
| A clearly defined keep rule | **Last write wins** on `(contract, frequency, ts)`, in both scopes above | ✅ |
| Correct aggregation | one bar per instant, so `daily_ohlcv` volume no longer double-counts and `rolling_vwap` emits strictly increasing timestamps | ✅ |
| Auditability | full row + `winning_row_hash` in `superseded_bars` | ✅ |

**Changing the rule.** Last-write-wins follows how vendors normally issue
restatements. Alternative rules (highest volume wins, or agree-with-the-daily-file)
would be a change to one function, `_resolve_instant_conflicts`.

**In the UI.** Duplicates surface as two different findings, and neither is
read from `bars` — the store cannot contain either by the time the dashboard
asks.

| Finding | Severity badge | Occurrences column | Evidence columns |
|---|---|---|---|
| `exact_duplicate` | `error` | copies − 1 | `row_hash`, `copies`, then the bar's OHLCV |
| `resolved_instant_conflict` | `warning` | distinct instants | `reason`, then the **discarded** row's OHLCV |

The second row is the one worth expanding: its evidence comes from
`superseded_bars`, so it is the only table in the dashboard showing values that
no longer exist anywhere in `bars`. `row_hash` is 40 characters and would
stretch every other column, so the cell is truncated and the full value is on
the `title` attribute.

> **Existing databases.** The bar primary key changed from `row_hash` to
> `(contract, frequency, ts)`. `CREATE TABLE IF NOT EXISTS` cannot alter a table
> that already exists, so `initialise()` detects the old key and raises with
> instructions. The store is a rebuildable artefact: delete
> `backend/data/market_data.duckdb` and re-ingest.

---

## 3. Malformed records — invalid prices and volumes

**Caught in:** quality layer only — [`checks/values.py`](../backend/src/market_data/domain/quality/checks/values.py),
rule `invalid_values`, category `invalid_value`. These rows **stay in `bars`** and
continue to feed aggregation; they are only flagged.

| code | Condition | Severity |
|---|---|---|
| `high_lt_low` | `high < low` | ERROR |
| `open_out_of_range` | open outside `[low, high]` | ERROR (minute) / WARNING (daily) |
| `close_out_of_range` | close outside `[low, high]` | ERROR (minute) / **INFO (daily)** |
| `non_positive_price` | `min(OHLC) <= 0` (includes negative prices) | ERROR |
| `non_finite_price` | inf / nan | ERROR |
| `negative_volume` | `volume < 0` | ERROR |
| `missing_volume` | minute bar with null volume (minute only) | WARNING |

### The logic: one row, one predicate

Every check in this module is a **single-row predicate**. A bar is judged on its
own fields, with no reference to its neighbours or to the time axis — that is
what separates this file from its siblings: `gaps.py` looks at the spacing
between bars, `duplicates.py` at bars sharing an instant, `outliers.py` at a bar
against the distribution around it. The checks are data, not control flow: each
is a `(code, severity, predicate, message)` tuple, so adding one is adding a row
to a list.

The predicates, as they are written:

```
high_lt_low          high < low
open_out_of_range    open  > high  or  open  < low
close_out_of_range   close > high  or  close < low
non_positive_price   min(open, high, low, close) <= 0
non_finite_price     any of open, high, low, close is inf or nan
negative_volume      volume < 0
missing_volume       volume is null                      (minute only)
```

Three details of how they are written matter:

- `min_horizontal(OHLC) <= 0` catches a zero and a sign flip in one pass. Zero
  is the fingerprint of a placeholder or an upstream null filled in; a negative
  price is a sign error.
- `volume < 0` is null-propagating in Polars, so a null volume makes the
  comparison null and the filter drops it. `negative_volume` therefore can never
  double-report a bar already caught by `missing_volume`.
- The predicates are **not mutually exclusive**. A bar with `high < low` will
  usually trip both range checks as well, and is reported by each. One bad bar
  can thus appear in three findings; the counts are per check, not per bar.

**Severity is context-aware:** a daily `close` outside `[low, high]` is legitimate
settlement behaviour → INFO, and its message says so; the same on a minute bar is
an ERROR.

```
check                 daily      minute
open_out_of_range     WARNING    ERROR
close_out_of_range    INFO       ERROR
missing_volume        --         WARNING
```

A minute bar's open *is* its first trade, so it cannot sit outside the minute's
own range; a daily open may come from an auction or a different session
convention. A daily futures close is usually the exchange settlement price,
computed from a closing window or a fair-value model, and legitimately lands
outside the traded range.

**Counting.** One summary issue per contract per code, with
`count = number of matching bars`, `start_ts = min(ts)` and `end_ts = max(ts)`
over the hits. `context.example_ts` carries the first hit in *frame* order,
which on an unsorted frame need not be the earliest one.

**In the UI.** Each code is one row in the Findings table, badged `error`,
`warning` or `info` from the matrix above, with the predicate's message in the
Detail column (`3 bar(s): high < low`). Expanding it lists the offending bars,
and the evidence columns are simply the bar's own `open`, `high`, `low`,
`close`, `volume` — because here the diagnosis *is* the numbers. "high < low"
means nothing until you see `high = 1.0, low = 99.0` on the row. Every
occurrence is a single instant, so the detail table drops its **Through**
column.

"Flag separately" is satisfied. Note these rows are **not** excluded from
aggregation — the brief asks only that they be flagged, so this is consistent —
but a negative price, say, still flows into `daily_ohlcv` / `vwap` with only an
ERROR in the report to signal it.

---

## 4. Missing timestamps and data gaps

**Detected in:** [`checks/gaps.py`](../backend/src/market_data/domain/quality/checks/gaps.py),
rule `timestamp_gaps`, category `gap`. Nothing is rejected or repaired here: an
absence has no row to act on, so it is reported and nothing else.

### The logic: diff the timestamps, then explain them away

Gaps are found by **diffing consecutive timestamps**, not by generating an
expected grid and subtracting. A grid needs a trading calendar, and there is no
calendar in this project; a diff needs only the data. Everything below exists to
stop that diff from drowning the user in *legitimate* absences — the dataset is
full of them.

**Step 1 — the window.** Start at the contract's first traded bar
(`_active_start`): the first `ts` where `volume > 0`, falling back to the first
bar if the contract never traded. A futures contract is listed long before it
attracts flow, and the quiet stretch before that is not a data gap.

**Step 2 — infer the bar interval.** Deduplicate on `ts`, sort, and take the
most common positive spacing:

```
gap_s_i  = t_i - t_{i-1}                      (seconds)
expected = mode({ gap_s_i : gap_s_i > 0 })
```

The **mode**, not the mean or the minimum. A handful of huge overnight gaps
would drag an average away from the real bar size, and the minimum would be
distorted by any near-duplicate timestamp. This is also why a 5-minute file is
graded against 300s without being told: 60s is inferred, never assumed.

**Step 3 — candidates.** Ordinary jitter in a thin book is not a gap, so only
spacing well past the interval is even considered:

```
candidate  ⟺  gap_s > gap_min_multiple × expected        (gap_min_multiple = 1.5)
```

**Step 4 — classify.** Severity is decided by whether the gap **crosses a
calendar-day boundary in the exchange timezone** (`America/Chicago`), not by its
duration. With `gap_days = date(t_i, tz) − date(t_{i-1}, tz)` and Polars
weekdays (1 = Mon … 7 = Sun), the four buckets are disjoint and evaluated in
this order:

| bucket | condition | code | severity |
|---|---|---|---|
| weekend | `gap_days ≥ 1` and (`prev_wd ≥ 5` or `ts_wd = 1`) | `session_break` | INFO |
| overnight | `gap_days ≥ 1` and `gap_s ≤ 36h` | `session_break` | INFO |
| extended | not weekend and `gap_s > 36h` | `extended_gap` | WARNING |
| intra | `gap_days = 0` | `intra_session_gap` | WARNING |

`prev_wd ≥ 5` is **Friday**, not Saturday, on purpose: the futures week runs
Sunday evening to Friday afternoon Chicago, so a normal weekend gap begins on
the Friday bar. The 36h line (`1.5 × 86,400`) only separates "extended" from the
rest; it is deliberately generous, because the span from one session's last bar
to the next session's first is well over the exchange's own 60-minute halt.

`intra_session_gap` is the bucket that matters — a hole inside a live session,
which no calendar explains.

**Step 5 — size the hole.** A gap sits *between* two bars, so the missing run
starts one interval after the last bar present and ends one interval before the
next:

```
missing_bars(gap)      = floor(gap_s / expected) − 1
first_missing          = t_{i-1} + expected
last_missing           = t_i − expected
estimated_missing_bars = Σ (gap_s / expected − 1)   over the intra bucket
```

For a gap of a single bar the two endpoints collapse to the same instant.

**Daily data is a different question** and gets a different formula. There is no
intraday spacing to infer; every *weekday* between the first and last observed
session should carry a bar:

```
missing_sessions = { d : first ≤ d ≤ last, weekday(d) ≤ 5 } \ { observed trading_date }
```

Exchange holidays are **not** excluded — there is no trading calendar — so
Thanksgiving and Christmas are counted. The finding says so in its own message
rather than overstating itself.

**Guards.** The minute classifier needs at least 10 spacings before it will
infer anything, and returns nothing when the inferred interval is zero or when
no spacing clears the threshold. The daily one needs at least 5 observed
sessions before a business-day diff means anything.

### In the UI

Gaps appear in **two places**, answering two different questions.

**The Findings table** answers *how bad is it*. One row per code per contract:

| code | Occurrences | Detail column | Evidence columns |
|---|---|---|---|
| `intra_session_gap` | number of gaps | `N intra-session gaps (~M missing 60s bars) for CLM26` | `missing bars`, `gap minutes` |
| `extended_gap` | number of gaps | `N multi-day gaps for CLM26 (holiday or data outage)` | `missing bars`, `gap minutes` |
| `session_break` | weekend + overnight | `N expected session breaks (overnight/weekend) for CLM26` | `missing bars`, `gap minutes` |
| `missing_sessions` | absent business days | `N business days with no bar … (some are exchange holidays)` | none; the date is the row |

Gap evidence is the only kind that is **synthesised** rather than pointing at a
bar, and the only kind that spans a range — so this is where the detail table's
**Through** column appears, showing `first_missing → last_missing`. Rows are
ranges rather than one row per absent minute: a thin contract can be short six
figures of bars, and enumerating each would bury the reader in rows that say
nothing `missing_bars` does not.

`session_break` is listed even though it is INFO. "We saw these and considered
them normal" is only checkable if the user can see which ones — and it is
exactly where the two misclassifications above surface.

`missing_sessions` uses `label` instead of `ts`, so the **When** column renders
`2024-11-28` rather than a meaningless `00:00` timestamp.

**The Missing timestamps section** answers the flatter question: *which instants
are gone*, one row each, from `GET /quality/missing-timestamps`. It lists only
`MinuteGaps.unexpected` (intra-session plus extended), because weekends and the
daily halt would bury the real holes under a quarter of a million rows saying
the market was shut. Minute absences are grouped under a Chicago date heading and
rendered as Chicago clock-time chips — a flat column of 500 near-identical timestamps is
unreadable, whereas under a date the times alone carry the information. Daily
absences need no grouping and render as one row of date chips. Chip tone comes
from the classification: `intra-session` is the suspicious one, `extended` is
often a holiday.

---

## 5. Outliers

**Detected in:** [`checks/outliers.py`](../backend/src/market_data/domain/quality/checks/outliers.py),
category `outlier`. Two rules, both advisory: an outlier is a signal to look,
not a defect. Neither excludes a bar from anything.

Both ask the same question — *is this bar far from the bars around it?* — and
both answer with a **robust** estimator, one whose value is not moved by the
very points it is meant to find.

### 5a. Price returns — median / MAD robust z-score (WARNING)

```
r_t        = ln(close_t) − ln(close_{t-1})
med        = median(r)
MAD        = median(|r − med|)
robust_z_t = |r_t − med| / (1.4826 × MAD)
flag       ⟺  robust_z_t > outlier_mad_threshold        (default 8.0)
```

**Why log returns.** They are symmetric in direction — a halving and a doubling
are the same distance from zero — which keeps the distribution centred and the
deviation measure meaningful.

**Why MAD and not the standard deviation.** The classic z-score divides by the
sample standard deviation, which is computed from the squared distance of every
point to the mean. A single print 500× too large therefore inflates the
denominator it is being tested against, widening the threshold until the outlier
looks ordinary and the merely-bad prints beside it are hidden completely. That
is the masking effect, and on a minute series containing a fat-finger tick it is
not hypothetical. MAD's breakdown point is 50%: up to half the sample can be
arbitrarily corrupted before it moves at all.

**Why the 1.4826.** MAD is on a different scale from σ, so a raw ratio to it
would not mean anything familiar. For Gaussian data MAD converges to
`0.6745 × σ` (0.6745 being the 75th-percentile z), so scaling by
`1 / 0.6745 = 1.4826` makes `1.4826 × MAD` a consistent estimator of σ. That is
what lets `robust_z` be read as "how many standard deviations out", in units an
analyst already has intuition for.

**Why 8 and not 3.** Intraday futures returns are strongly leptokurtic; a
3-sigma minute happens many times a day in normal trading, so a textbook cutoff
would flag thousands of good bars and tell the user nothing. Eight is set where
a hit is more likely to be a bad print than a market move.

**Guards.** At least 30 bars per contract, `close > 0` (a non-positive close is
already an invalid-value finding and here would only produce a NaN), and the
contract is skipped when `MAD = 0` — that happens when over half the returns are
identical, a locked or untraded stretch, and dividing by it would put every
non-flat bar infinitely far out.

### 5b. Volume — ratio to a rolling local median (INFO)

```
local_med_t = rolling_median(volume, window = 50, min_samples = 20)
flag        ⟺  local_med_t > 0  and  volume_t > volume_spike_threshold × local_med_t
x_median    = volume_t / local_med_t                    (threshold default 20.0)
```

**Why a ratio and not the same robust z.** Volume is non-negative and heavily
right-skewed rather than roughly symmetric, and on thin minute data more than
half the bars can be identical (often zero), which drives MAD to zero and makes
a z-score undefined exactly where it is needed. A multiple of the median has
none of those failure modes and states the finding the way a trader would:
"twenty times normal".

**Why rolling and not one median for the series.** Volume has a strong intraday
profile — the open and the close are many times the midday rate — so a single
median would flag every open and miss every quiet-hour anomaly. Fifty bars is
long enough to be stable and short enough to track the profile;
`min_samples = 20` lets the check start early instead of blanking the first 49
bars. The window is trailing and includes the current bar, so a spike nudges its
own baseline — by one observation in fifty, which cannot move a median enough to
matter.

**Guards.** At least 50 bars, non-zero total volume, and `local_med > 0`, which
drops both the warm-up nulls and dead stretches where any non-zero bar would be
an infinite multiple. Null volume is filled with 0 here so it cannot break the
rolling median; it is a separate finding (`missing_volume`).

**Why INFO.** A volume spike is usually a real market event — a data release, a
roll, an expiry — so it is context for the reader rather than a defect.

**Thresholds are settings, not constants.** `outlier_mad_threshold` and
`volume_spike_threshold` live in `Settings`, so tuning them is not a code
change.

### In the UI

| Finding | Badge | Evidence columns |
|---|---|---|
| `price_return_outlier` | `warning` | `log return`, `mad score`, then `open`/`high`/`low`/`close`/`volume` |
| `volume_spike` | `info` | `local median`, `x median`, then the bar's OHLCV |

Both carry the score **on the row**, not just as a mask, so the table shows *how
far out* each bar was rather than only that it crossed. Both then trail the
bar's own OHLCV, because a log return of 0.11 could be a bad print or a genuine
move and only the prices say which; `80,000 lots` says nothing without the
baseline it is being compared against, which is what `local median` is for.

`price_return_outlier` also carries `mad_threshold` and `max_abs_return` in
`context`, so the cutoff in force when the report ran is recoverable without
rerunning the check.

Fractional figures are the reason the detail table formats cells by type rather
than by column name: a log return of 0.063 through a whole-number formatter
would render as `0`.

---

## The quality report

`QualityService.report()` → `QualityReport`, with issues sorted severity-first:

```
contract: "GCM26"  frequency: "minute"  bars_checked: 12345
issues: [
  QualityIssue(category="invalid_value", code="high_lt_low", severity="error",
               contract="GCM26", frequency="minute", count=3,
               start_ts=..., end_ts=...,
               message="3 bar(s): high < low", context={"example_ts": "..."}),
  QualityIssue(category="conflict", code="resolved_instant_conflict", severity="warning",
               contract="GCM26", frequency="minute", count=2,
               context={"discarded_rows": 2, "reasons": ["superseded_in_file"]}),
  ...
]
```

- API: `GET /quality/report?contract=GCM26&frequency=minute`, which the
  dashboard's Data quality tab renders as a findings table. `contract` is
  required; `start` and `end` default to the contract's full stored range.
- Rules live in the `checks/` directory and are listed explicitly by
  `all_rules()`; adding a check is writing one function and naming it there.

Categories emitted by the built-in rules: `gap`, `duplicate`, `conflict`,
`invalid_value`, `outlier`. `IssueCategory` also declares `timestamp`, reserved
for a future rule; nothing raises it today. Severities: `info`, `warning`,
`error`.

---

## Per-occurrence evidence

A finding is a summary: *"9,594 intra-session gaps"*. That is enough to raise an
eyebrow and not enough to act on, so each issue can also carry the occurrences
behind it as `IssueDetail` rows, which the dashboard renders when a finding is
expanded.

| Field | Meaning |
|---|---|
| `ts` | where the occurrence starts |
| `end_ts` | set only when it spans a range; `None` for a single instant |
| `label` | preferred over `ts` when a rule's unit is not an instant (a daily session is a *date*) |
| `values` | free-form per-occurrence figures; the UI renders one column per key it finds |
| `detail_total` | how many occurrences exist |
| `detail_truncated` | true when `details` holds only the first `len(details)` of them |

Counts are unbounded, so the report caps its list at `Settings.issue_detail_limit`
(500) and reports the real total alongside. That cap is a *sampling* limit, not a
ceiling: `GET /quality/issue-details?code=…` pages past it, and the dashboard's
detail pager uses it. The check is re-run against a settings copy whose cap
covers the requested window and the page is sliced out, which keeps rules pure
and holds no per-session state on the server.

Everything is assembled through
[`domain/quality/details.py`](../backend/src/market_data/domain/quality/details.py),
which keeps the three fields consistent.

Every check is wired. What each one puts in `values`:

| Check | Evidence per occurrence |
|---|---|
| `intra_session_gap`, `extended_gap`, `session_break` | `missing_bars`, `gap_minutes`, spanning `ts` → `end_ts` |
| `missing_sessions` | one row per absent date, in `label` |
| `high_lt_low` and the other invalid-value checks | the bar's `open`/`high`/`low`/`close`/`volume` |
| `price_return_outlier` | `log_return`, `mad_score` |
| `volume_spike` | `volume`, `local_median`, `x_median` |
| `exact_duplicate` | `row_hash`, `copies` |
| `conflicting_duplicate` | `variants`, `rows` |
| `repeated_timestamp` | `rows` |
| `resolved_instant_conflict` | the **discarded** row's OHLCV plus its `reason` |

Every check that points at a real bar also carries that bar's
`open`/`high`/`low`/`close`/`volume` (`BAR_VALUE_COLUMNS`), after its own
figures. A log return of 0.11 could be a bad print or a genuine move, and only
the prices say which. The aggregate rules (`exact_duplicate`,
`conflicting_duplicate`, `repeated_timestamp`) group before they report, so the
values are joined back from a representative row — exact where every copy is
identical, and the first variant only for a conflicting instant, which is why
`variants` stays on the row.

Two of these are worth calling out.

The invalid-value checks put the offending numbers on screen because the
diagnosis *is* the numbers: "high < low" means nothing until you see
`high = 1.0, low = 99.0`.

`resolved_instant_conflict` shows what the row that *lost* the tie-break
actually said. Those values are in `superseded_bars` and nowhere else, so this
is the one drill-down surfacing data no other view in the dashboard can reach.

Gaps are the odd ones out: an absence has no row to point at, so they synthesise
their evidence. Everything else already holds a frame of offending bars and goes
through `bar_details`.

---

## Listing the missing timestamps

`GET /quality/missing-timestamps` answers the flatter question the report does
not: *which instants have no bar*, one row each. The dashboard renders it as its
own section under the findings.

It is a separate endpoint because of volume. A report holds thousands of gaps;
the instants inside them run to hundreds of thousands for a single thin
contract — `CLM26` in the sample data is short 898,622 minute bars. So the
response is **paged**, not capped: `total`, `offset` and a `limit` the client
walks, because "show every missing timestamp" is only true if you can reach the
end of the list.

**What is listed.** Only absences the exchange calendar does not explain, which
is `MinuteGaps.unexpected` — intra-session plus extended gaps. Weekends and the
daily halt are not missing data, and including them would bury the real holes
under a quarter of a million rows saying the market was shut. Each row carries a
`classification` (`intra_session`, `extended` or `missing_session`) so the UI can
distinguish a hole in a live session from a probable holiday.

**One source of truth.** The classification is not re-derived here.
[`missing.py`](../backend/src/market_data/domain/quality/missing.py) calls
`classify_minute_gaps` and `missing_sessions` from
[`checks/gaps.py`](../backend/src/market_data/domain/quality/checks/gaps.py) —
the same functions the gap rule uses — so the listing can never disagree with the
finding it drills into. The expansion itself is vectorised (`datetime_ranges`
into a list column, then exploded), which keeps a 238k-instant expansion at well
under a tenth of a second.

---

## The UI tables

Every finding the quality layer raises renders through **one** Angular component,
[`quality/quality.html`](../frontend/src/app/quality/quality.html), and three
tables. There is no per-category branch anywhere in it: a new check appears in
the dashboard without a line of frontend code, which is the point of pushing the
column names into `IssueDetail.values`.

**Four stat tiles** sit above everything: bars checked, errors, warnings,
informational. The three severity tiles sum `issue.count`, **not** the number of
findings — one finding covering 9,594 gaps contributes 9,594. The error and
warning tiles take their colour only when non-zero, so a clean contract shows
four neutral tiles rather than a wall of green.

**Table 1 — Findings.** One row per finding, sorted severity-first by the API.

| Column | Source |
|---|---|
| Severity | `issue.severity`, as a coloured badge |
| Category | `issue.category` — `gap`, `duplicate`, `conflict`, `invalid_value`, `outlier` |
| Check | `issue.code`, in monospace: the handle you would grep for |
| Occurrences | `issue.count`, and what an occurrence *is* differs per rule (see each section above) |
| First seen / Last seen | `issue.start_ts` / `issue.end_ts`, rendered in Chicago time on minute data; on daily data the date alone, since a daily `ts` is a date stamped at 00:00 UTC rather than an instant |
| Detail | `issue.message`, plus a **Show N occurrences** button when `detail_total > 0` |

Clicking anywhere on an expandable row toggles it. Only one finding is open at a
time, keyed by `code`.

**Table 2 — Evidence**, nested inside the expanded row. Its columns are the
**union of the `values` keys across the fetched rows, in first-seen order**, so
each rule decides its own schema and the UI needs no case analysis. Keys are
de-underscored for the heading, so `missing_bars` reads as "missing bars". Three
rendering rules keep it honest across such different payloads:

- **When / Through.** `label` wins over `ts` when a rule set one (a daily
  session is a date, not an instant). The **Through** column appears only when
  some row actually spans a range, which in practice means gaps.
- **Alignment by type, not by name.** A column is right-aligned when any row
  holds a number there, decided per column so cells do not jitter, and from the
  data rather than a hard-coded key list.
- **Formatting by type.** Integers go through the thousands formatter; anything
  fractional is printed as-is, because a log return of 0.063 would otherwise
  round to `0`. Long strings such as a 40-character `row_hash` are truncated in
  the cell with the full value on `title`.

It pages 50 rows at a time against `GET /quality/issue-details`, and paints
immediately from the sample already in the report while the first page is in
flight. The footer always states `from–to of total`, so a truncated sample can
never be mistaken for the whole set.

**Table 3 — Missing timestamps**, its own section and its own request. Not a
drill-down, because the volumes are incomparable: the findings table holds
thousands of gaps, the instants inside them hundreds of thousands. Minute
absences are chips grouped under a Chicago date heading with a per-day count; daily
absences are one flat row of date chips. Tone splits `intra-session` (suspicious)
from `extended` (often a holiday), and a legend says which is which. It pages
500 at a time, and the empty state says out loud that weekends and the daily
halt are excluded by design — otherwise "no missing timestamps" would look like
a bug.

---

## What the findings add up to

The report says *what* is wrong and how often. Whether 9,000 gaps are the daily
halt, a thin book or an outage is a separate question, answered on request by
the *Intelligent insights* section below the findings: it re-runs the validator
with every occurrence, summarises each finding into distributions, and asks an
LLM for recurring patterns and suggested cleansing or validation rules. See
[insights.md](insights.md).
