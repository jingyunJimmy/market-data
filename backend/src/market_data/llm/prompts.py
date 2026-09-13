"""What the model is told, and the JSON shapes it must answer in.

The prompts carry the domain knowledge the evidence cannot: how these markets
trade, and how each quality check decides what to flag. Without it a model
cannot tell a scheduled halt from an outage, which is the whole question.

The suggestion schema is built from the catalogue, so each type's parameter
names and choices are enforced as Claude writes the answer.
:mod:`market_data.domain.insights.verify` only checks the JSON shape, so the
grounding rules stated here are guidance the model is asked to follow, not
something enforced afterwards.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from market_data.domain.insights.catalogue import CATALOGUE, ParamKind, ParamSpec, SuggestionSpec
from market_data.domain.insights.models import (
    Confidence,
    EvidencePack,
    Pattern,
    PatternClassification,
    SuggestionKind,
)

_CONTEXT = """\
## The data
Historical futures bars (minute or daily) for one contract. All clock times are
exchange time, America/Chicago ("CT").

## How these markets trade
- CME Globex products trade Sunday-Friday 17:00-16:00 CT with a daily
  maintenance halt from 16:00 to 17:00 CT. The weekend closure runs from Friday
  16:00 to Sunday 17:00 CT. Some products, such as ICE softs, trade a shorter
  day session.
- Exchange holidays close or shorten sessions.
- Back-month contracts trade thinly: minutes with no trades produce no bar.
- Liquidity migrates between contracts around a roll.

## How the quality checks decide what to flag
- intra_session_gap: bar spacing above gap_min_multiple x the modal bar
  interval, within one CT calendar date. The 16:00-17:00 CT halt does not cross
  midnight, so it is filed here.
- session_break: a gap crossing a CT date (overnight or weekend). Informational.
- extended_gap: a gap over 36 hours that is not a weekend.
- missing_sessions: a weekday with no daily bar. Holidays are not excluded.
- price_return_outlier: log return whose robust z-score (median/MAD) exceeds
  outlier_mad_threshold. Returns are taken between adjacent rows, even across a gap.
- volume_spike: volume above volume_spike_threshold x the rolling 50-bar median.
- high_lt_low, open_out_of_range, close_out_of_range, non_positive_price,
  non_finite_price, negative_volume, missing_volume: single-row checks.
- exact_duplicate, repeated_timestamp: identical rows delivered again.
- conflicting_duplicate, resolved_instant_conflict: one instant, different
  values. Ingestion keeps the latest row and audits the rest.

## The evidence fields
occurrences (the finding's count), analysed (rows the statistics came from),
per_1k_bars, days_affected and days_affected_share (of trading days in scope),
top_hours (busiest CT hours with share of analysed), weekday_counts,
median_duration_min and p90_duration_min (gaps), max_magnitude with
magnitude_metric (how extreme), share_after_gap (share landing within
after_gap_window_min after a gap ends), period_counts (trend), and
expected_interval_s (bar interval gaps were measured against)."""

_FIGURE_RULES = """\
- Quote figures exactly as they appear in the evidence you cite. A share may be
  written as a percentage. Do not compute new numbers: no sums, differences,
  ratios or unit conversions.
- Write clock times as HH:MM, 24-hour, CT. Write dates as YYYY-MM-DD.
- Reply with JSON only, matching the schema."""

PATTERNS_SYSTEM = f"""\
You are a market-data quality analyst. You receive an evidence pack of
statistics computed from a data-quality report, and you identify recurring
data-quality patterns in it.

{_CONTEXT}

## What to return
A pattern is a regularity in how occurrences are distributed: concentrated in
one time window, present on most trading days, following gaps, trending, or
repeatedly restated by the source. A count on its own is not a pattern.

For each pattern:
- id: P1, P2, ...
- evidence_refs: the evidence ids (E1, E2, ...) it rests on. Only ids in the pack.
- contracts: only contracts those evidence rows cover.
- classification, one of:
  - expected_market_behavior: normal for how the market trades; should stop being reported.
  - data_source_defect: the vendor feed is wrong.
  - threshold_miscalibration: a check is too tight or too loose for this data.
  - ingestion_artifact: produced by how the data was loaded.
  - unknown: the evidence cannot decide.
- confidence: high only when the distribution is unambiguous.
- title: under ten words. explanation: two or three sentences saying what the
  distribution shows and what it most likely means.

{_FIGURE_RULES}
- If nothing recurs, return an empty list."""

SUGGESTIONS_SYSTEM = f"""\
You are a market-data quality analyst. You receive an evidence pack, the
data-quality patterns already verified against it, and a catalogue of rule
changes this system supports. You propose cleansing or validation rules.

{_CONTEXT}

## What to return
For each pattern, zero to two suggestions:
- pattern_id: the id of a pattern you were given.
- type: a catalogue type. Prefer a specific type; use custom only when none fits.
- params: exactly the parameter names the catalogue lists for that type.
  - code: a finding code the pattern's evidence covers.
  - contract: one of the pattern's contracts.
  - for adjust_threshold, params.value must be one specific number, never a
    range, direction ("higher", "looser") or qualitative description. It must
    govern the pattern's findings and differ from that setting's current value
    in the pack's thresholds.
- kind: cleansing (changes what data is used) or validation (changes what is
  reported). Ignored for every type except custom.
- rationale: one or two sentences on why this rule follows from the pattern.
  For adjust_threshold, state the exact current and proposed values (e.g.
  "raise outlier_mad_threshold from 6 to 9") rather than describing the change
  in words alone.

Typical fits: expected_market_behavior -> expected_window;
data_source_defect -> reject_at_ingest, exclude_from_analytics or dedupe_policy;
threshold_miscalibration -> adjust_threshold.

{_FIGURE_RULES}"""


def patterns_prompt(pack: EvidencePack) -> str:
    return (
        f"Evidence pack:\n{_json(pack.model_dump(mode='json', exclude_defaults=True))}\n\n"
        "Identify the patterns."
    )


def suggestions_prompt(pack: EvidencePack, patterns: Sequence[Pattern]) -> str:
    return (
        f"Evidence pack:\n{_json(pack.model_dump(mode='json', exclude_defaults=True))}\n\n"
        f"Verified patterns:\n{_json([p.model_dump(mode='json') for p in patterns])}\n\n"
        f"Catalogue:\n{_json(_catalogue())}\n\n"
        "Propose the suggestions."
    )


def patterns_schema() -> dict[str, Any]:
    item = _object(
        {
            "id": {"type": "string"},
            "title": {"type": "string"},
            "classification": {"type": "string", "enum": [c.value for c in PatternClassification]},
            "contracts": {"type": "array", "items": {"type": "string"}},
            "evidence_refs": {"type": "array", "items": {"type": "string"}},
            "explanation": {"type": "string"},
            "confidence": {"type": "string", "enum": [c.value for c in Confidence]},
        }
    )
    return _object({"patterns": {"type": "array", "items": item}}, title="patterns")


def suggestions_schema() -> dict[str, Any]:
    """One variant per catalogue type, each spelling out that type's parameters.

    ``params`` cannot be a bare ``{"type": "object"}``: structured outputs close
    every object (``additionalProperties: false``), and a closed object with no
    listed properties has only one possible value, ``{}`` -- a suggestion with
    no window, no value and no rule in it.
    """
    item = {"anyOf": [_suggestion_variant(spec) for spec in CATALOGUE.values()]}
    return _object({"suggestions": {"type": "array", "items": item}}, title="suggestions")


def _suggestion_variant(spec: SuggestionSpec) -> dict[str, Any]:
    kinds = [spec.kind.value] if spec.kind else [k.value for k in SuggestionKind]
    return _object(
        {
            "pattern_id": {"type": "string"},
            # An enum of one rather than `const`: the SDK's schema transform keeps
            # `enum` but demotes `const` to description text, which is not enforced.
            "type": {"type": "string", "enum": [spec.type.value]},
            "kind": {"type": "string", "enum": kinds},
            "params": _object({p.name: _param_schema(p) for p in spec.params}),
            "rationale": {"type": "string"},
        }
    )


def _param_schema(param: ParamSpec) -> dict[str, Any]:
    if param.choices:
        return {"type": "string", "enum": list(param.choices), "description": param.description}
    kind = "number" if param.kind is ParamKind.NUMBER else "string"
    return {"type": kind, "description": param.description}


def _catalogue() -> list[dict[str, Any]]:
    return [
        {
            "type": spec.type.value,
            "kind": spec.kind.value if spec.kind else "your choice",
            "description": spec.description,
            "params": [
                {
                    "name": p.name,
                    "description": p.description,
                    **({"choices": list(p.choices)} if p.choices else {}),
                }
                for p in spec.params
            ],
        }
        for spec in CATALOGUE.values()
    ]


def _object(properties: dict[str, Any], *, title: str | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        # LangChain's structured-output helper needs a name for the schema it
        # sends; only the top-level call passes one.
        **({"title": title} if title else {}),
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def _json(value: object) -> str:
    # Compact: the pack is read by a model, and whitespace is tokens.
    return json.dumps(value, separators=(",", ":"))
