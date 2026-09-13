"""The rule changes a suggestion may propose, and what each one needs.

This table is what the LLM prompt lists as the closed set of types a
suggestion may use, and what the suggestion JSON schema is built from, so a
model can only pick a type from here and must fill in exactly that type's
parameters, from ``choices`` where there are any.
:mod:`market_data.domain.insights.verify` does not check the parameter values
against it -- whether a window or a value is right is left to the model,
guided by the descriptions below.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from market_data.domain.insights.models import SuggestionKind, SuggestionType


class ParamKind(StrEnum):
    CONTRACT = "contract"
    """One of the pattern's contracts."""
    CODE = "code"
    """A finding code the pattern's evidence covers (and in ``choices``, when given)."""
    TIME = "time"
    """``HH:MM``, 24-hour, exchange time."""
    NUMBER = "number"
    """A positive number."""
    TEXT = "text"
    CHOICE = "choice"


@dataclass(frozen=True)
class ParamSpec:
    name: str
    kind: ParamKind
    description: str
    choices: tuple[str, ...] = ()


@dataclass(frozen=True)
class SuggestionSpec:
    type: SuggestionType
    kind: SuggestionKind | None
    """Fixed by the type, or ``None`` when the provider chooses (``custom`` only)."""
    description: str
    params: tuple[ParamSpec, ...]


#: The quality settings a suggestion may tune, and the findings each governs.
#: Only the keys are used (as ``TUNABLE_SETTINGS``); the mapping records which
#: findings each setting governs, and nothing checks a suggestion against it.
SETTING_CODES: dict[str, frozenset[str]] = {
    "outlier_mad_threshold": frozenset({"price_return_outlier"}),
    "volume_spike_threshold": frozenset({"volume_spike"}),
    "gap_min_multiple": frozenset({"intra_session_gap", "extended_gap", "session_break"}),
}
TUNABLE_SETTINGS: tuple[str, ...] = tuple(SETTING_CODES)

#: Findings whose occurrences sit at a time of day, so a window can explain them.
WINDOW_CODES = ("intra_session_gap", "volume_spike", "price_return_outlier")

#: Single-row predicates, and therefore checkable before a row is stored.
INGEST_REJECTABLE_CODES = (
    "high_lt_low",
    "open_out_of_range",
    "close_out_of_range",
    "non_positive_price",
    "non_finite_price",
    "negative_volume",
    "missing_volume",
)

DEDUPE_CODES = ("exact_duplicate", "repeated_timestamp", "conflicting_duplicate", "resolved_instant_conflict")
DEDUPE_POLICIES = ("keep_first", "keep_last", "flag_only")

CATALOGUE: dict[SuggestionType, SuggestionSpec] = {
    spec.type: spec
    for spec in (
        SuggestionSpec(
            SuggestionType.EXPECTED_WINDOW,
            SuggestionKind.VALIDATION,
            "Treat a recurring time-of-day window as expected, so occurrences inside it stop being reported.",
            (
                ParamSpec("contract", ParamKind.CONTRACT, "the contract the window applies to"),
                ParamSpec("code", ParamKind.CODE, "the finding the window explains", WINDOW_CODES),
                ParamSpec("start_ct", ParamKind.TIME, "window start, HH:MM exchange time"),
                ParamSpec("end_ct", ParamKind.TIME, "window end, HH:MM exchange time"),
            ),
        ),
        SuggestionSpec(
            SuggestionType.ADJUST_THRESHOLD,
            SuggestionKind.VALIDATION,
            "Change a quality threshold that is too tight or too loose for this data.",
            (
                ParamSpec("setting", ParamKind.CHOICE, "the setting to change", TUNABLE_SETTINGS),
                ParamSpec(
                    "value",
                    ParamKind.NUMBER,
                    "one specific proposed value, not a range or direction, different from the current one",
                ),
            ),
        ),
        SuggestionSpec(
            SuggestionType.EXCLUDE_FROM_ANALYTICS,
            SuggestionKind.CLEANSING,
            "Keep flagged bars out of daily OHLCV and VWAP while still reporting them.",
            (ParamSpec("code", ParamKind.CODE, "the finding whose bars to exclude"),),
        ),
        SuggestionSpec(
            SuggestionType.REJECT_AT_INGEST,
            SuggestionKind.VALIDATION,
            "Reject rows failing a single-row check at ingest, recording them in rejected_rows.",
            (ParamSpec("code", ParamKind.CODE, "the single-row check to enforce", INGEST_REJECTABLE_CODES),),
        ),
        SuggestionSpec(
            SuggestionType.DEDUPE_POLICY,
            SuggestionKind.CLEANSING,
            "Choose which row survives when the source repeats or restates an instant.",
            (
                ParamSpec("code", ParamKind.CODE, "the duplicate or conflict finding", DEDUPE_CODES),
                ParamSpec("policy", ParamKind.CHOICE, "which row to keep", DEDUPE_POLICIES),
            ),
        ),
        SuggestionSpec(
            SuggestionType.CUSTOM,
            None,
            "Anything the types above cannot express. Needs an engineer to design.",
            (ParamSpec("description", ParamKind.TEXT, "the rule, in one or two sentences"),),
        ),
    )
}
