# Intelligent Insights

The brief asks the application to **identify recurring data-quality patterns**
and **suggest cleansing or validation rules**. The quality report already says
*what* is wrong and how often. Insights say what it adds up to: whether 9,000
gaps are a scheduled halt, a thin book or an outage, and what to change as a
result.

The approach is AI-assisted: the statistics are computed deterministically, and
an LLM reads them and proposes patterns and rules. Both are trusted as the
model states them, past a schema check -- a malformed item (a missing field,
the wrong type, an enum value that does not exist) is refused, but nothing is
checked against the statistics themselves. The evidence is shown beside every
pattern and suggestion so a reader can check the model's reasoning directly.

- [The flow](#the-flow)
- [1. The evidence pack](#1-the-evidence-pack)
- [2. Patterns](#2-patterns)
- [3. Verification](#3-verification)
- [4. Suggestions](#4-suggestions)
- [The LLM](#the-llm)
- [Configuring the LLM](#configuring-the-llm)
- [The API and the dashboard](#the-api-and-the-dashboard)
- [What the evidence shows on the real dataset](#what-the-evidence-shows-on-the-real-dataset)

## The flow

```
QualityReport            (module ③)
      │
      ▼
① evidence pack          evidence.py
      │
      ▼
② patterns               LLM
      │
      ▼
③ verify                 verify.py
      │
      ▼
④ suggestions            LLM
      │
      ▼
⑤ verify                 verify.py
```

| Step | Who | Where |
|---|---|---|
| Run the validator with a raised evidence cap | code | `services/insights_service.py` |
| ① Summarise each finding into distributions | code | `domain/insights/evidence.py` |
| ② Identify recurring patterns, each citing evidence ids | LLM | `services/insights_service.py` |
| ③ Keep only well-formed patterns | code | `domain/insights/verify.py` |
| ④ Suggest rules for the patterns that survived | LLM | `services/insights_service.py` |
| ⑤ Keep only well-formed suggestions | code | `domain/insights/verify.py` |

Suggestions are requested **after** patterns pass schema validation, so a rule
is proposed only for a pattern the model actually returned in a usable shape.

`InsightsService` builds each prompt and calls
[`ClaudeClient.complete_json`](../backend/src/market_data/llm/claude.py) directly
-- there is no adapter class between the service and the model. The domain
(`domain/insights/`) still never imports `llm/`: it only defines the evidence,
the pattern/suggestion shapes and verification, all of which are plain data and
functions. Tests hand the service a scripted object with the same `model` and
`complete_json` in the client's place.

## 1. The evidence pack

The model never sees bars or raw occurrences, only this pack. Each non-empty
finding in the report becomes one `Evidence` row with an id (`E1`, `E2`, ...):

| Field | Meaning | Why it matters |
|---|---|---|
| `occurrences`, `analysed` | the finding's count; the occurrence rows the statistics came from | a capped distribution must not pass for a complete one |
| `per_1k_bars` | occurrences per 1,000 bars checked | separates "a lot" from "a lot for this much data" |
| `days_affected`, `days_affected_share` | distinct sessions hit, and their share of the sessions checked | **recurring** means most sessions, not one bad afternoon |
| `top_hours` | the three busiest exchange-time hours with their share | a scheduled event concentrates in one hour |
| `weekday_counts`, `period_counts` | by weekday; by week, month or quarter depending on the span | day-of-week effects and trends |
| `median_duration_min`, `p90_duration_min` | gap length | a 61-minute gap every day is a halt |
| `max_magnitude`, `magnitude_metric` | the most extreme `mad_score` or `x_median` | how far out the worst hit was |
| `share_after_gap` | share of occurrences within 5 minutes after a gap ends | an outlier measured across missing bars is measuring the gap |
| `expected_interval_s` | the bar interval gaps were measured against | lets a threshold be proposed in the right unit |

The pack also carries the scope (contract, frequency, range, bars checked,
trading days, time zone) and the **thresholds in force**, so a suggestion to
change one can be checked against its current value.

Three details decide whether the distributions are right:

- **The full list, not the dashboard's sample.** The report the dashboard shows
  caps each finding at `issue_detail_limit` (500) occurrences. A distribution
  drawn from 500 of 9,500 gaps would describe the sample, so the service reruns
  the validator with the cap raised to `insights_max_occurrences` (50,000).
- **Exchange time.** Hours and dates are counted in `America/Chicago`. 21:00 UTC
  is the 16:00 CT halt; counted in UTC it would look like nothing in particular.
  A daily finding keeps its session date rather than being shifted onto the
  previous evening.
- **"After a gap" starts where bars resume.** A gap detail's `end_ts` is the last
  *missing* bar, so the series resumes one interval later. An as-of join finds
  each occurrence's nearest preceding resume.

## 2. Patterns

A pattern is a regularity in how occurrences are distributed, not a restated
count. Each one names the evidence it rests on and says what it most likely
means:

| Classification | Meaning | Typical rule |
|---|---|---|
| `expected_market_behavior` | normal for how the market trades; should stop being reported | `expected_window` |
| `data_source_defect` | the vendor feed is wrong | `reject_at_ingest`, `exclude_from_analytics`, `dedupe_policy` |
| `threshold_miscalibration` | a check is too tight or too loose for this data | `adjust_threshold` |
| `ingestion_artifact` | produced by how the data was loaded | `dedupe_policy` |
| `unknown` | the evidence cannot decide | usually `custom` |

Plus a `title`, an `explanation`, the `contracts` it concerns and a
`confidence`.

## 3. Verification

A draft that fails to parse against the schema is returned as a `Rejection`
with a reason, not repaired and not silently dropped -- one malformed item
costs only itself. The dashboard lists what was removed, because seeing the
refusals is how a reader learns what was kept.

Both **patterns** and **suggestions** are checked for schema only: required
fields present, the right types, an enum value that actually exists in
`PatternClassification`, `SuggestionType`, `SuggestionKind`, and so on.
Nothing beyond that is checked -- a pattern's evidence refs, contracts and
figures, and a suggestion's attachment to a kept pattern, its parameters
against the catalogue, and the figures in its rationale, are all taken as the
model states them. The evidence pack travels with the report precisely so a
reader can check that reasoning themselves.

## 4. Suggestions

The rule changes a suggestion can propose are a **closed set**, defined once in
[`catalogue.py`](../backend/src/market_data/domain/insights/catalogue.py). The
prompt lists it and asks the model to follow it, but verification only checks
that `type` is one of these values -- not the parameters or constraints below,
which are guidance to the model rather than something enforced afterwards.

| Type | Kind | Parameters | Constraint the prompt asks the model to honour |
|---|---|---|---|
| `expected_window` | validation | `contract`, `code`, `start_ct`, `end_ct` | `code` cited and one of `intra_session_gap`, `volume_spike`, `price_return_outlier`; a non-empty window |
| `adjust_threshold` | validation | `setting`, `value` | `setting` governs a cited finding; `value` positive and different from the current one |
| `exclude_from_analytics` | cleansing | `code` | cited |
| `reject_at_ingest` | validation | `code` | cited, and a single-row check |
| `dedupe_policy` | cleansing | `code`, `policy` | a cited duplicate or conflict finding; `keep_first`, `keep_last` or `flag_only` |
| `custom` | the model's choice | `description` | non-empty; labelled in the UI as needing an engineer to design |

*Cleansing* changes what data is used; *validation* changes what is reported.

Suggestions are **proposals**. Nothing applies them. `adjust_threshold` is the
one that maps directly onto configuration today (the `MARKET_DATA_*` threshold
variables). The others name a change in a known place — the gap check, the
ingest predicates, the dedupe step — rather than a switch that already exists.

## The LLM

`InsightsService` makes two calls through
[`ClaudeClient`](../backend/src/market_data/llm/claude.py), built on
[LangChain's `ChatAnthropic`](https://python.langchain.com/docs/integrations/chat/anthropic/):
one for patterns, then one for suggestions on the verified patterns.

- **Structured output.** `with_structured_output(schema, method="json_schema")`
  drives Claude's own schema-constrained output feature
  (`output_config.format`), so the answer arrives as schema-valid JSON -- the
  same JSON Schema dicts `prompts.py` builds are passed straight through, no
  Pydantic model needed.
- **Streaming under the hood.** The client is constructed with
  `streaming=True`, so every call runs through the streaming API even though
  `complete_json` returns one parsed answer: an evidence pack is a long input,
  and thinking can make the answer slow to start, which a synchronous call
  can time out waiting on.
- **The prompt carries what the evidence cannot.** How the markets trade — the
  Globex session, the 16:00–17:00 CT halt, weekends, thin back months, rolls —
  and how each check decides what to flag, for example that the halt lands in
  `intra_session_gap` because it does not cross midnight. It also asks the
  model to ground its patterns and suggestions in the evidence and follow the
  catalogue, though none of that is checked afterwards -- only the JSON shape
  is. See [`prompts.py`](../backend/src/market_data/llm/prompts.py).
- **What leaves the process** is the evidence pack, the catalogue and the
  verified patterns: aggregates, ids, counts and timestamps, never bars.

**Failure is not the same as being wrong.** If the model produces no usable
answer at all, `ClaudeClient` raises `InsightProviderError`. The route answers
**503** with the reason, and the dashboard shows it. That covers:

- a refusal (`stop_reason == "refusal"`);
- an answer cut off at `max_tokens`;
- a parsing failure (LangChain's JSON parser tolerates partial JSON, so a
  cut-off answer is caught by the `stop_reason` check first, not by parsing);
- a 404 (bad model), 429 (rate limit), 401/other API error, or an unreachable
  endpoint.

There is no deterministic fallback: an empty report would read as "no patterns
found", which is a claim nobody made. A malformed item is refused on its own,
one by one, so it does not take the rest of a usable answer down with it.

## Configuring the LLM

```bash
MARKET_DATA_LLM_API_KEY=sk-ant-...
poetry run market-data-serve
```

`MARKET_DATA_LLM_MODEL` defaults to `claude-sonnet-5`, and
`MARKET_DATA_LLM_MAX_TOKENS` (default 16000) caps each answer.
`MARKET_DATA_LLM_TIMEOUT_S` (default 180) bounds each request. If
`MARKET_DATA_LLM_API_KEY` is unset, LangChain falls back to the
`ANTHROPIC_API_KEY` environment variable. API errors, refusals and truncated
answers all surface as the same 503 with a reason.

Only Claude is wired up today, and `InsightsService` is typed to `ClaudeClient`.
Pointing this at a different model means writing a client with the same `model`
property and `complete_json(system, user, schema)` method over that provider's
LangChain chat model (`ChatOpenAI`, `ChatOllama`, etc. from the matching
`langchain-*` package) the way `llm/claude.py` does for `ChatAnthropic`,
building it in `api/deps.py`, and widening the type the service accepts.

A local model would suit market data well: free, no key, and the evidence stays
on the machine. The evidence pack keeps what a model has to read down to a few
kilobytes of statistics rather than raw data, but a weaker model's mistakes
about those numbers would not be caught -- only a malformed answer is.

## The API and the dashboard

`POST /insights?contract=…&frequency=…&start=…&end=…` returns an
`InsightsReport`:

- `scope`: what was examined;
- `model`: the model that read the evidence;
- `generated_at`;
- `evidence`: the rows patterns cite;
- `patterns`;
- `suggestions`;
- `rejected`: what verification refused, and why.

It is a `POST` because a run does real work and asks an LLM. A `GET` is refused
with 405. When the model cannot answer, the response is 503 with the reason in
`detail`. An empty range returns an empty report without calling the model.

On the **Data quality** tab, the *Intelligent insights* section sits below the
findings and the missing-timestamp listing:

- **Nothing runs until the button is pressed.** The store pins each request to
  the selection it was made for. Moving a filter drops it immediately, and
  returning to the same selection does not generate again by itself.
- **Figures come from the evidence rows,** rendered under each pattern — not
  from the model's explanation, so they are right even when the wording is not.
- **Each suggestion shows its type, its kind and its parameters.** A custom rule
  reads as a sentence instead.
- **Output is labelled** *AI-assisted* with the model's name.
- **A failed run shows the API's reason,** for example that the endpoint is
  unreachable.
- **Refused items stay visible.** Anything verification removed is listed in a
  collapsible *removed by verification* note.

## What the evidence shows on the real dataset

What the model is given to reason from, on the ingested sample data (each pack
computed in under 0.25 s):

| Contract, range | Bars | What the evidence shows |
|---|---|---|
| **GCK26** minute, 30 days to 2026-05-27 | 520 | 363 gaps on 21 of 23 trading days, 698.08 per 1,000 bars, median gap 7.0 min, no hour above 12 % — a thin book. Every one of the 23 return outliers and 8 volume spikes lands within 5 min after a gap. |
| **SBK25** minute, full history | 131,146 | 34,007 gaps on 648 of 674 trading days, median 3.0 min. 95 % of 282 return outliers and 63 % of 1,734 volume spikes follow a gap. 31 multi-day gaps. |
| **ESZ25** minute, 30 days to 2025-12-19 | 29,589 | 307 return outliers, 10.38 per 1,000 bars, the largest at a MAD score of 41.7. 144 intra-session gaps, the busiest hour (16:00 CT) holding only 12 % of them — not a clean halt signature. |
| **CLM26** daily, a year to 2026-05-19 | 252 | 10 business days without a bar. |
