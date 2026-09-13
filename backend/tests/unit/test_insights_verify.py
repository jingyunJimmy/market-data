"""Insights step 3: what verification accepts and what it refuses.

Both patterns and suggestions are checked for schema only -- a provider's
reading of the evidence is trusted as given, for both. Only a malformed
draft (a missing field, the wrong type, an enum value that does not exist)
is refused, and it must be caught on its own, without taking the rest of the
answer down with it.
"""

from __future__ import annotations

import pytest

from market_data.domain.insights.verify import verify_patterns, verify_suggestions


def _pattern(**over: object) -> dict[str, object]:
    return {
        "id": "P1",
        "title": "Gaps recur at 16:00 CT",
        "classification": "expected_market_behavior",
        "contracts": ["ESZ25"],
        "evidence_refs": ["E1"],
        "explanation": "91% of 9,594 gaps start at 16:00 CT, on 20 of 27 trading days.",
        "confidence": "high",
        **over,
    }


def _suggestion(**over: object) -> dict[str, object]:
    return {
        "pattern_id": "P1",
        "type": "expected_window",
        "kind": "validation",
        "params": {"contract": "ESZ25", "code": "intra_session_gap", "start_ct": "16:00", "end_ct": "17:00"},
        "rationale": "The 16:00-17:00 CT halt is scheduled.",
        **over,
    }


# --------------------------------------------------------------------------- #
# Patterns
# --------------------------------------------------------------------------- #


def test_a_well_formed_pattern_is_kept():
    """The baseline every refusal below is measured against: a complete draft passes untouched."""
    kept, rejected = verify_patterns([_pattern()])

    assert [p.id for p in kept] == ["P1"]
    assert rejected == []


def test_one_malformed_pattern_costs_only_itself():
    """An enum value that does not exist is refused with the field named; the good item beside it survives."""
    kept, rejected = verify_patterns([_pattern(classification="spooky"), _pattern(id="P2")])

    assert [p.id for p in kept] == ["P2"]
    assert len(rejected) == 1
    assert rejected[0].stage == "pattern" and rejected[0].item == "P1"
    assert "classification" in rejected[0].reason


# --------------------------------------------------------------------------- #
# Suggestions
# --------------------------------------------------------------------------- #


def test_a_well_formed_suggestion_is_kept():
    """The suggestion baseline: a complete draft passes, its params exactly as the provider sent them."""
    kept, rejected = verify_suggestions([_suggestion()])

    assert rejected == []
    assert kept[0].params == _suggestion()["params"]


@pytest.mark.parametrize(
    ("over", "where"),
    [
        ({"type": "not_a_type"}, "type"),
        ({"kind": "not_a_kind"}, "kind"),
        ({"rationale": None}, "rationale"),
    ],
    ids=["bad-type", "bad-kind", "null-rationale"],
)
def test_a_malformed_suggestion_is_rejected_with_the_reason(over, where):
    """A bad type, a bad kind or a null rationale each fail the schema, and each reason names its field."""
    kept, rejected = verify_suggestions([_suggestion(**over)])

    assert kept == []
    assert rejected[0].stage == "suggestion"
    assert where in rejected[0].reason


def test_one_malformed_suggestion_costs_only_itself():
    """The suggestion twin of the pattern case: the bad item is refused, the good one beside it survives."""
    kept, rejected = verify_suggestions([_suggestion(type="not_a_type"), _suggestion(pattern_id="P2")])

    assert [s.pattern_id for s in kept] == ["P2"]
    assert len(rejected) == 1
