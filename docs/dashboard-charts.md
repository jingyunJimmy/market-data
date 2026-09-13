# Dashboard Charts & VWAP

Every visualisation the dashboard renders, what it is showing, where the numbers
come from, and how the rolling VWAP is computed.

## Time zone

The API and the database are UTC throughout. The dashboard shows **every date
and time in Chicago time** (CT: UTC-6 in winter, UTC-5 in summer), and says so
once, in a note under the page title, rather than on every card. Chicago is the
calendar the page is already keyed on: the date filter and `trading_date` are
Chicago dates (see Chart 1), so a chart and the filter above it agree on where a
day starts and ends.

The conversion happens only at display time, in
[`core/time.ts`](../frontend/src/app/core/time.ts). Its `EXCHANGE_TZ`
(`America/Chicago`) must match the backend's `MARKET_DATA_DAILY_BAR_TZ`;
nothing enforces that today.

| What | Shown as |
|---|---|
| Date filter, daily candles, volume bars, session table | the Chicago date (`trading_date`), so no conversion is needed |
| VWAP chart axis and crosshair | Chicago wall-clock time (see Chart 2) |
| Findings and evidence timestamps, minute data | Chicago date and time, e.g. `03/06/2024, 09:00` |
| Findings and evidence timestamps, daily data | the date alone, unshifted, e.g. `2024-06-03` |
| Missing timestamps, minute data | grouped under the Chicago date, with Chicago clock times |

Daily timestamps are the exception because they are not instants: a daily `ts`
is a date stamped at 00:00 UTC, and converting it to Chicago would print 18:00
the evening before, which reads as a different session.

## The two tabs

One filter bar drives both tabs: **contract**, **frequency**, **start**, **end**,
and the **VWAP window**. Nothing is per-tab, so switching tabs never refetches
or loses your selection ([`store.ts`](../frontend/src/app/core/store.ts) is a
root-provided singleton).

| Tab | Charts | Tables | Endpoints |
|---|---|---|---|
| **Dashboard** | Daily OHLCV candlesticks with volume · rolling VWAP | Session detail | `/analytics/daily-ohlcv`, `/analytics/vwap` |
| **Data quality** | none (findings are tabular by nature) | Findings · per-finding evidence · missing timestamps | `/quality/report`, `/quality/issue-details`, `/quality/missing-timestamps` |

Both tabs also show four headline figures across the top.

---

## Chart 1 — Daily OHLCV candlesticks

**Component:** `<app-chart [series]="candles()" [height]="420" />` in
[`analytics.html`](../frontend/src/app/analytics/analytics.html).
**Source:** `GET /analytics/daily-ohlcv?contract=…&start=…&end=…&source=<frequency>`.

One candle per **trading session**. What a session means depends on the
frequency filter, and the card header says which you are looking at:

| Frequency filter | What the candle is | `bar_count` |
|---|---|---|
| `minute` | minute bars aggregated into a session by the backend | number of minute bars, e.g. 1380 |
| `daily` | the vendor's own daily bar, passed straight through | 1 |

The aggregation itself is `daily_ohlcv` in
[`daily_bars.py`](../backend/src/market_data/domain/analytics/daily_bars.py),
grouping by `(contract, trading_date)`:

| field | rule |
|---|---|
| `open` | first non-null open, earliest `ts` in the session |
| `high` | session maximum |
| `low` | session minimum |
| `close` | last non-null close, latest `ts` in the session |
| `volume` | sum |
| `open_interest` | last observed |
| `bar_count` | number of source bars |

Because `bars` holds one row per instant, these aggregates are trustworthy:
`volume` cannot double-count a repeated timestamp, `high` and `low` cannot be
dragged out by a duplicate variant, and `close` is deterministic. That is
enforced at ingest, not here; see
[data-quality.md §2b](data-quality.md#2-duplicate-records).

`trading_date` is the bar's **Chicago calendar date**, not its UTC date. It comes
from the file's own `trading_date` column when present; otherwise the backend
converts `ts` to `America/Chicago` (`MARKET_DATA_DAILY_BAR_TZ`) before taking the
date, so a Chicago evening is not split at midnight UTC. It is a calendar date,
not the CME session date: in the sample dataset, CLG26's bars from the Sunday
17:00 CT open carry Sunday's date rather than Monday's (see
[Known issues](#known-issues)).

**Time axis format.** Candles pass `trading_date` through as a plain
`YYYY-MM-DD` string, which lightweight-charts reads as a business day. The axis
therefore shows dates only, with no clock time (`intraday` is left `false`).
Because the key is already a Chicago date, the axis needs no time-zone
conversion.

**Null handling.** Bars missing any of O/H/L/C are filtered out in the
`candles()` computed before reaching the chart, because a candlestick needs all
four. Such a bar would only exist here if it were aggregated from an incomplete
session; structurally incomplete rows never reach the database at all (see
[data-quality.md](data-quality.md)).

### Volume bars

The same chart draws each session's volume as a histogram along the bottom, so a
volume bar sits directly under its candle. It is a third series kind, `volume`,
in [`chart.ts`](../frontend/src/app/shared/chart.ts), built by the same
`candles()` computed in
[`analytics.ts`](../frontend/src/app/analytics/analytics.ts).

| Aspect | Rule |
|---|---|
| Key | `trading_date`, the same business-day string as the candles |
| Value | the session's summed `volume` |
| Colour | red when the session closed below its open, green otherwise, both at 45 % opacity so the band stays secondary to price |
| Placement | its own price scale (`priceScaleId: 'volume'`), confined to the bottom 25 % of the pane; the candle scale gets a matching bottom margin, so candles and bars never overlap |
| Nulls | filtered on `volume` alone, independently of the candles: a session dropped for a missing price keeps its volume bar, and a session with no volume draws no bar rather than a zero-height one |

A session missing its open or close is coloured green, because "closed down"
cannot be established without both.

---

## Chart 2 — Rolling VWAP vs typical price

**Component:** `<app-chart [series]="vwapSeries()" [height]="300" [intraday]="true" />`.
**Source:** `GET /analytics/vwap?contract=…&start=…&end=…&window_minutes=…`.

Two line series on one pane:

| Series | Colour | Meaning |
|---|---|---|
| VWAP | `#1f5fd6` (blue) | the trailing volume-weighted average |
| Typical price | `#9aa4b5` (grey) | that single bar's own `(H+L+C)/3`, not weighted by volume |

Plotting both is the point of the panel. The grey line is the raw per-bar price
and the blue line is the volume-weighted trailing average, so the gap between
them shows where volume is concentrated. Price running above VWAP on thin
volume looks very different from the same move on heavy volume.

**This panel only exists on minute data.** On `daily` the store withholds the
request entirely and the card renders an explanation instead, because a trailing
intraday window over daily bars is not a defined quantity.

**Time axis format.** Unlike the candles, VWAP points carry a real instant.
lightweight-charts has no time-zone support and labels every timestamp as UTC,
so `analytics.ts` hands it **Chicago wall-clock seconds** rather than true epoch
seconds: `exchangeWallSeconds(iso)` in
[`core/time.ts`](../frontend/src/app/core/time.ts) adds that instant's Chicago
offset (-6 h or -5 h, looked up per date from the browser's time-zone data).
`2024-06-03T14:00:00Z` is therefore plotted at `09:00`, which is what the axis
and crosshair then read. This shift is the approach the library's own docs
recommend. The chart is created with `timeVisible: true` so the axis shows clock
time.

**Truncation is visible.** The card header prints the point count and appends
"showing the latest N" whenever the API sets `truncated`. The series is capped
rather than thinned, so what you see is a contiguous recent window, never a
subsampled approximation of the whole range.

**Timestamps are strictly increasing, and must be.** lightweight-charts calls
`checkItemsAreOrdered` from `setData` without `allowDuplicates`, so two points
sharing a time value fail an assertion: in a development build the panel throws
and renders nothing, and in a production build the assertion is stripped and the
behaviour is undefined. Nothing upstream can produce that any more, because
ingestion stores one bar per instant and `rolling_vwap` emits one point per bar
(see [data-quality.md §2b](data-quality.md#2-duplicate-records)). Before that
invariant existed, conflicting duplicates fanned out into extra points and broke
this chart.

The Chicago shift adds one more way to repeat a time: the hour Chicago falls back
from daylight saving happens twice on the wall clock. `ascending()` in
`analytics.ts` drops the second pass of that hour rather than let the chart
throw. For CME contracts it never removes real data, because the repeated hour is
01:00–02:00 on a Sunday, inside the weekend closure.

---

## How VWAP is calculated

The pure function is `rolling_vwap` in
[`vwap.py`](../backend/src/market_data/domain/analytics/vwap.py). For each bar
at time `t`:

```
                 Σ (typical_price_i × volume_i)
  vwap(t)  =  ──────────────────────────────────      for  t - W < ts_i ≤ t
                      Σ (volume_i)

  typical_price = (high + low + close) / 3
```

`W` is the window, 15 minutes by default (`MARKET_DATA_VWAP_WINDOW_MINUTES`,
overridable per request). The dashboard offers 5, 15, 30 and 60.

### Why `(H+L+C)/3`

A minute bar tells you the range that traded but not the distribution inside it.
The three-price mean is the conventional futures proxy for the bar's centre of
trading. `open` is excluded because it largely repeats the previous bar's close
and would double-count that instant.

### Worked example

From `backend/data/fixtures/clean_minute.csv` with a 3-minute window:

| ts (UTC) | typical_price | volume | vwap |
|---|---|---|---|
| 15:00 | 78.006667 | 326 | 78.006667 |
| 15:01 | 78.050000 | 439 | 78.031534 |
| 15:02 | 78.063333 | 361 | 78.041729 |
| 15:03 | 78.103333 | 57 | 78.059164 |

The first bar's VWAP equals its own typical price: the window contains only
itself. Checking the fourth bar by hand, its window `(15:00, 15:03]` covers
15:01, 15:02 and 15:03, giving `Σpv = 66896.7033` over `Σvol = 857`, so
`vwap = 78.0591637495`. The function returns `78.05916374951381`.

### Three decisions that matter

**The window is measured in time, not in rows.**
`rolling_sum_by("ts", window_size="15m", closed="right")` is a true time window.
This is not optional on this dataset: the
minute series is full of legitimate holes for overnight breaks, weekends,
holidays, and the thin period before a contract becomes the front month. A
fixed "previous 15 rows" window would quietly reach back across a weekend and
average Friday's close into Monday's open, and nothing in the output would look
wrong.

**`closed="right"` makes the interval half-open, `(t-W, t]`.** A bar sitting
exactly on the `t-W` boundary is excluded, so a 15-minute window never covers 16
sampling points. More importantly the window contains only the current bar and
past bars, never a future one, so the series carries no look-ahead and is safe
to use as a backtest input.

**`.over("contract")` keeps windows inside one contract.** A query spanning
several contracts computes each independently; no window ever straddles two
instruments.

The sums are computed with the **expression** form, `rolling_sum_by(...).over(...)`,
rather than a frame-level `rolling(...).agg(...)`. The expression form yields one
value per input row, so every bar keeps its own `typical_price` beside its window
sums. The `agg` form collapses the window and would need a join back on
`(contract, ts)` to recover that price, and such a join fans out into a cartesian
product wherever a timestamp carries conflicting duplicates.

### Zero and null volume

Both are real conditions in this data, and they are handled differently.

- **Zero window volume** yields `null`, not an error and not zero. Deferred
  contracts produce runs of settlement-only bars with `volume = 0` and
  `O = H = L = C`; a volume-weighted average over them is undefined. The chart
  simply breaks the line at those points.
- **Null volume on a bar** is coerced to `0` (`fill_null(0)`), so the bar
  contributes to neither numerator nor denominator. Without this, one null would
  poison the whole window sum. The bar itself is still flagged
  `missing_volume` (WARNING) by the quality layer.

### Tests

| test | asserts |
|---|---|
| [`test_rolling_vwap_matches_independent_computation`](../backend/tests/unit/test_analytics.py) | every point matches a naive Python recomputation of its window |
| [`test_vwap_within_window_price_range`](../backend/tests/unit/test_analytics.py) | **Hypothesis** property: VWAP always lies within the window's `[min low, max high]`, the invariant any weighted mean must satisfy |
| `test_rolling_vwap_zero_volume_is_null` | a zero-volume bar yields `null` |
| `test_conflicting_duplicates_are_both_weighted_into_the_window` | two bars on one instant both contribute to the window sum, each at its own typical price and volume |
| `test_vwap_reports_window_and_truncation` | the API echoes `window_minutes` and flags `truncated` |

### Where the cap is applied

`rolling_vwap` returns one point per bar. `AnalyticsService.vwap` then takes
`frame.tail(limit)` and sets `truncated`, because a wide range on a liquid
contract is hundreds of thousands of points. The API defaults `limit` to 50,000
and allows up to 200,000.

---

## The non-chart panels

**Headline figures** (`<app-stat>`), four per tab. They are computed in the
browser from data already fetched, so they add no requests.

| Dashboard | Data quality |
|---|---|
| Sessions (candle count) | Bars checked |
| Last close | Errors |
| Range change, first to last close, in percent | Warnings |
| Total volume | Informational |

The quality counts are summed over each finding's `count`, not over the number
of findings, because one finding can cover many bars. The range-change tile is
tinted green or red by sign.

**Session detail** table repeats the candle data numerically, adding the
`bar_count` column so you can see how many minute bars each session was built
from. A session with far fewer bars than its neighbours is usually a half-day.

**Findings** table lists every quality issue with severity, category, check
code, occurrence count, first and last timestamp, and the message. On minute
data the timestamps are shown as Chicago date and time. On daily data they show
the date alone, unshifted, for the reason given under [Time zone](#time-zone).

---

## Chart rendering mechanics

All charting is confined to [`chart.ts`](../frontend/src/app/shared/chart.ts), a
single component with three inputs:

| input | purpose |
|---|---|
| `series` | one or more `ChartSeries`, a discriminated union of `candlestick`, `line` and `volume` |
| `height` | pixel height, applied as a host style binding |
| `intraday` | show clock time on the axis rather than dates alone |

The library is **lightweight-charts v5**, about 60 kB gzipped. Note that v5
replaced v4's `addCandlestickSeries()` / `addLineSeries()` with a single
`addSeries(SeriesType, options)` call taking the series type as the first
argument; v4 examples found online will not compile against this code.

Lifecycle is deliberate. The chart is created in `afterNextRender` because
`createChart` needs a real DOM node; an `effect` redraws when `series` changes;
a second `effect` toggles the time axis; and `DestroyRef.onDestroy` calls
`chart.remove()`. Panels therefore contain no imperative charting code at all,
they only map API data into `ChartSeries` objects.

Redraw is a full replacement: every existing series handle is removed and
re-added, then `timeScale().fitContent()` reframes the view. That is more work
than patching data in place, but the panels change their entire dataset on every
filter change anyway, so incremental updates would add bookkeeping for no gain.

**One maintenance trap.** The chart colours are hard-coded as hex constants at
the top of `chart.ts` and duplicate the `--up` / `--down` / `--accent` tokens in
[`styles.css`](../frontend/src/styles.css). Canvas rendering cannot read CSS
custom properties, so the duplication is necessary, but the two lists have to be
changed together. The volume colours in `analytics.ts` repeat the same green and
red at 45 % opacity, a third copy to keep in step.

---

## Edge cases in the charts UI

A market-data dashboard spends a lot of its life pointed at something other than
a clean, fully populated range: a filter combination with no rows behind it, a
backend that is not running, a session missing its volume, a window over which
VWAP is undefined. The rule throughout is that **the UI never draws an empty or
half-true chart**. Either it has enough data to draw honestly, or it says in
words what is missing.

### The four-state ladder

Every chart panel renders exactly one of four states, in this order, and the
order matters: a failed request also has no data, so the error branch has to be
tested before the empty branch or a backend outage would read as "no bars in
this range".

| State | Condition | What is shown |
|---|---|---|
| Loading | `resource.isLoading()` | "Loading bars…" / "Computing VWAP…" |
| Failed | `resource.error()` | a red placeholder naming the request that failed |
| Empty | no rows in the response | "No bars in the selected range." |
| Drawn | otherwise | the chart |

The `<app-chart>` element is inside the last branch only, so on the first three
it is never instantiated. Nothing constructs a chart in order to leave it blank.
The session-detail table goes further and is omitted **including its header**
when `bars()` is empty ([`analytics.html`](../frontend/src/app/analytics/analytics.html)),
because a header row with nothing under it reads as a load that stopped halfway.

### Requests that are never sent

An empty state is cheaper if the request never happens. Each resource takes a
reactive query that returns `undefined` when the request would be meaningless,
and `httpResource` skips the fetch entirely for as long as it is:

| Guard | In | Effect |
|---|---|---|
| no contract, or no start/end date yet | `barsQuery` | nothing is fetched during first paint, before the catalogue lands |
| frequency is `daily` | `vwapQuery` | the VWAP panel explains itself instead of asking for an undefined quantity |
| no finding expanded | `detailQuery` | evidence is fetched on expansion, not for every finding in the report |

So the daily-frequency case is not an empty chart that happens to have no
points. It is a fifth state, checked before loading, that renders the sentence
"A trailing intraday window needs minute bars."

### Failures above the charts

Two conditions make every panel meaningless at once, so they are handled in the
shell ([`app.html`](../frontend/src/app/app.html)) and the tabs are not rendered
at all:

- **API unreachable.** `store.unreachable()` is true when the catalogue request
  itself errored. The notice names the command that starts the backend.
- **Nothing ingested.** The catalogue succeeded and came back empty. The notice
  names the ingestion command. It is distinguished from still-loading by
  `catalogue.isLoading()`, so a slow first request does not accuse the user of
  having an empty database.

### Filters that cannot express an invalid range

The date inputs constrain each other and the data. `start` takes `min` from the
selected contract's `first_date` and `max` from the current `end`; `end` takes
`min` from `start` and `max` from `last_date`. An inverted range, or one outside
the contract's history, cannot be picked in the first place. Changing contract
or frequency re-runs `resetRange`, which re-opens the default window against the
new selection rather than leaving dates pointing at another instrument's
history. The VWAP window select is `disabled` on daily rather than being left
live over a panel that is not drawing.

### Missing values inside data that did arrive

Nulls are real in this dataset and each series decides for itself what to do
with one. Nothing is zero-filled; a zero is a price, and inventing one is worse
than drawing nothing.

| Place | Null policy |
|---|---|
| Candles | a bar missing **any** of O/H/L/C is dropped from the series, because lightweight-charts cannot draw a partial candle |
| Volume bars | filtered on `volume` alone: a null draws no bar, and a session dropped from the candles still draws its volume |
| VWAP line | points with a null `vwap` are dropped, so the blue line breaks over a zero-volume window |
| Typical-price line | filtered **independently**, so the grey line stays continuous across that same gap |
| Session table | `format.price` / `format.count` render null as an em dash, never as `0` |
| Total volume | null coalesces to `0` **for the sum only**, so one session missing its volume does not void a range total |

The two VWAP lines being filtered separately is the subtle one: a zero-volume
run has a perfectly well-defined typical price and an undefined VWAP, and the
broken blue line against the continuous grey line is exactly the right picture
of that.

### Degenerate arithmetic in the headline figures

Each tile is a single number a reader takes at face value, so the guards are
about what the panel **declines** to compute:

- **Range change** needs at least two closes. One close is a level, not a
  change, and reporting `0.00%` would assert a flatness the data does not
  support. It also refuses when the first close is `0`, which would otherwise
  render as `Infinity%`.
- **Last close** falls back to `null`, and thence to a dash, on an empty range.
- **Tone** is `neutral` when the change is null, so a missing figure is never
  tinted green or red.

### Invariants the chart library enforces, and where they are met

lightweight-charts is strict about its input, and two of its assertions would
surface as a blank or throwing panel rather than as a readable error:

- **Strictly increasing timestamps.** `setData` runs `checkItemsAreOrdered`
  without `allowDuplicates`. This is met upstream: ingestion stores one bar per
  instant and `rolling_vwap` emits one point per bar, and the backend returns
  both series sorted. See the note under Chart 2 and
  [data-quality.md §2b](data-quality.md#2-duplicate-records).
- **Consistent time units.** Candles pass `trading_date` through as a business-day
  string; VWAP points are converted to Chicago wall-clock seconds. Mixing the two on one pane
  silently misplaces points, so the conversion is asserted in
  [`analytics.spec.ts`](../frontend/src/app/analytics/analytics.spec.ts).

Inside [`chart.ts`](../frontend/src/app/shared/chart.ts) three details cover the
remaining states. Drawing is guarded on the chart existing at all, since the
`series` effect can fire before `afterNextRender` has created it. Every previous
series handle is removed before the new ones are added, so a filter change that
returns fewer series leaves nothing stale on the pane. And `fitContent()` runs
only when at least one series has points, because fitting an empty pane throws
the time scale onto a meaningless default range. `chart.remove()` on destroy
keeps a tab switch from leaking a canvas.

### Where the edge cases are tested

The chart is stubbed in the component tests, which is what makes the boundary
assertable: what the panel hands the chart is precisely where the null handling
and the unit conversion live.

| test | asserts |
|---|---|
| `drops a bar that is missing any of its four prices` | incomplete candles never reach the series |
| `draws volume on the same chart, keyed by the same trading date` | volume is a `volume` series on the candle chart, keyed by `trading_date` |
| `keeps the volume of a session whose candle was dropped, and drops an absent volume` | volume is filtered independently of the candles, and null is not drawn as zero |
| `colours a volume bar by whether its session closed down` | red for a down session, green otherwise |
| `hands the chart Chicago wall-clock seconds, because it labels every time as UTC` | `14:00Z` in June is plotted at `09:00` |
| `drops the repeated hour when Chicago falls back from daylight saving` | the repeated wall-clock hour cannot reach `setData` as a duplicate time |
| `switches exactly at the daylight-saving boundary` ([`time.spec.ts`](../frontend/src/app/core/time.spec.ts)) | the per-hour offset cache does not smear the new offset into the minute before |
| `drops a point whose VWAP is undefined without dropping its typical price` | the two lines are filtered independently |
| `declines to compute a change from a single session` | no change from one close |
| `declines to divide by an opening close of zero` | no `Infinity%` |
| `sums volume across the range, treating an absent volume as none` | null volume does not void the total |
| `renders an absent figure as a dash rather than a zero` | table cells, checked cell by cell |
| `is omitted entirely when there are no bars` | the empty-range placeholder replaces the table |
| `explains itself on daily data instead of drawing an empty chart` | the daily branch, and that no chart is put in intraday mode |
| `asks for nothing until a contract and a range exist` | no request before the filters are complete |
| `reports the API as unreachable when the catalogue request failed` | the shell-level outage notice |

---

## Known issues

1. **A contract with only one frequency ingested.** The contract dropdown is
   built from distinct names across both frequencies, so selecting a frequency
   that has no data for the chosen contract leaves the charts empty without
   explaining why. Disabling unavailable frequency options would fix it.
2. **`trading_date` is a Chicago calendar date, not the CME session date.** Bars
   printed after the 17:00 CT open belong to the next CME trading day but carry
   the calendar day they print on. A Sunday evening therefore becomes its own
   "Sunday" candle, each weekday candle runs from midnight rather than from the
   previous evening's open, and a date filter starting on Monday misses Monday's
   first hours. Fixing it means rolling bars at or after the session open into
   the next trading day when `trading_date` is derived, then re-ingesting.
3. **VWAP at the start of a range comes from a partial window.** The date filter
   is applied before `rolling_vwap`, so the first `W` minutes of the range cannot
   see the bars just before it, and the same bar's VWAP changes with the chosen
   start date. Loading `W` minutes of warm-up before `start`, then dropping those
   points, would fix it.
4. **The display zone is set in two places.** `EXCHANGE_TZ` in
   [`core/time.ts`](../frontend/src/app/core/time.ts) and the backend's
   `MARKET_DATA_DAILY_BAR_TZ` must agree; the API does not send the zone to the
   client.
