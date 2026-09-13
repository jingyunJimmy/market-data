"""The LLM adapter: what ``ClaudeClient`` sends Claude, the prompts, and every
way an answer fails to arrive.

No model is called. ``ClaudeClient`` takes its LangChain chat model as a
parameter, so these stand a scripted one in its place -- one that answers
``with_structured_output`` the way LangChain's own helper would, rather than
reaching into Claude or LangChain internals.
"""

from __future__ import annotations

from types import SimpleNamespace

import anthropic
import httpx2 as httpx
import pytest
from anthropic import transform_schema

from market_data.domain.insights.catalogue import CATALOGUE
from market_data.domain.insights.models import (
    Confidence,
    EvidencePack,
    InsightScope,
    PatternClassification,
    SuggestionType,
)
from market_data.domain.models import Frequency
from market_data.llm import ClaudeClient, InsightProviderError
from market_data.llm.prompts import (
    SUGGESTIONS_SYSTEM,
    patterns_schema,
    suggestions_prompt,
    suggestions_schema,
)


class _Structured:
    def __init__(self, outcome: object) -> None:
        self._outcome = outcome

    def invoke(self, messages: list) -> object:
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


class _FakeLlm:
    """Stands in for the LangChain chat model: records the call, replays one outcome."""

    def __init__(self, outcome: object) -> None:
        self._outcome = outcome
        self.calls: list[dict] = []

    def with_structured_output(self, schema: dict, *, method: str, include_raw: bool) -> _Structured:
        self.calls.append({"schema": schema, "method": method, "include_raw": include_raw})
        return _Structured(self._outcome)


def _answer(parsed: object = None, *, stop_reason: str = "end_turn", parsing_error: object = None) -> dict:
    return {
        "raw": SimpleNamespace(response_metadata={"stop_reason": stop_reason}),
        "parsed": parsed,
        "parsing_error": parsing_error,
    }


def _ask(outcome: object) -> object:
    client = ClaudeClient(model="claude-sonnet-5", llm=_FakeLlm(outcome))
    return client.complete_json(system="sys", user="usr", schema=patterns_schema())


def _status_error(cls: type[anthropic.APIStatusError], code: int) -> anthropic.APIStatusError:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls("boom", response=httpx.Response(code, request=request), body=None)


# --------------------------------------------------------------------------- #
# The request
# --------------------------------------------------------------------------- #


def test_the_request_asks_for_claudes_native_structured_output():
    """json_schema drives Claude's own structured output; include_raw keeps the stop reason readable."""
    llm = _FakeLlm(_answer({"patterns": []}))
    client = ClaudeClient(model="claude-sonnet-5", llm=llm)

    assert client.complete_json(system="sys", user="usr", schema=patterns_schema()) == {"patterns": []}
    call = llm.calls[0]
    assert (call["method"], call["include_raw"], call["schema"]) == ("json_schema", True, patterns_schema())


# --------------------------------------------------------------------------- #
# Reading the answer
# --------------------------------------------------------------------------- #


def test_the_answer_is_the_parsed_structured_output():
    """On a clean end_turn the parsed object is returned as-is, with no reshaping by the client."""
    assert _ask(_answer({"patterns": [{"id": "P1"}]})) == {"patterns": [{"id": "P1"}]}


def test_a_refusal_is_a_provider_error():
    """Checked before parsing, so a refusal is reported as a refusal rather than as invalid JSON."""
    with pytest.raises(InsightProviderError, match="declined"):
        _ask(_answer(stop_reason="refusal"))


def test_an_answer_cut_off_at_max_tokens_is_a_provider_error():
    """LangChain's JSON parser tolerates partial JSON; the cut-off must still be caught."""
    with pytest.raises(InsightProviderError, match="cut off"):
        _ask(_answer({}, stop_reason="max_tokens"))


def test_a_parsing_failure_is_a_provider_error():
    """With include_raw, LangChain reports a parse failure beside the answer instead of raising it."""
    with pytest.raises(InsightProviderError, match="not valid JSON"):
        _ask(_answer(parsing_error=ValueError("bad json")))


def test_no_parsed_output_is_a_provider_error():
    """No parsing error but nothing parsed either is still no answer, and gets the same message."""
    with pytest.raises(InsightProviderError, match="not valid JSON"):
        _ask(_answer(parsed=None))


# --------------------------------------------------------------------------- #
# Every way of not reaching Claude at all
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("outcome", "message"),
    [
        (_status_error(anthropic.NotFoundError, 404), "not available"),
        (_status_error(anthropic.RateLimitError, 429), "rate limit"),
        (_status_error(anthropic.AuthenticationError, 401), "HTTP 401"),
        (
            anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com")),
            "unreachable",
        ),
    ],
    ids=["bad-model", "rate-limit", "bad-key", "offline"],
)
def test_every_failure_to_reach_claude_is_a_provider_error(outcome, message):
    """Each SDK failure becomes the one error the route maps to a 503, worded to say what to fix."""
    with pytest.raises(InsightProviderError, match=message):
        _ask(outcome)


# --------------------------------------------------------------------------- #
# The prompts
# --------------------------------------------------------------------------- #


def _pack(evidence: list | None = None) -> EvidencePack:
    return EvidencePack.model_validate(
        {
            "scope": InsightScope(
                contract="ESZ25",
                frequency=Frequency.MINUTE,
                start=None,
                end=None,
                bars_checked=100,
                trading_days=1,
                timezone="America/Chicago",
                after_gap_window_min=5,
            ),
            "thresholds": {"gap_min_multiple": 1.5},
            "evidence": evidence
            if evidence is not None
            else [
                {
                    "id": "E1",
                    "contract": "ESZ25",
                    "code": "intra_session_gap",
                    "category": "gap",
                    "severity": "warning",
                    "occurrences": 3,
                    "analysed": 3,
                    "per_1k_bars": 30.0,
                }
            ],
        }
    )


def test_the_schemas_offer_exactly_the_domain_enums():
    """If a classification or type is added to the domain, the model has to be offered it."""
    pattern = patterns_schema()["properties"]["patterns"]["items"]["properties"]
    variants = suggestions_schema()["properties"]["suggestions"]["items"]["anyOf"]

    assert pattern["classification"]["enum"] == [c.value for c in PatternClassification]
    assert pattern["confidence"]["enum"] == [c.value for c in Confidence]
    assert [v["properties"]["type"]["enum"] for v in variants] == [[t.value] for t in SuggestionType]


def test_each_suggestion_type_asks_for_its_catalogue_params_in_the_schema_claude_receives():
    """Structured outputs close every object, so params with no listed properties could only be {}.

    Checked after the SDK's own transform, since that is what is sent: it would,
    for one, demote a `const` to unenforced description text.
    """
    variants = transform_schema(suggestions_schema())["properties"]["suggestions"]["items"]["anyOf"]

    for variant, spec in zip(variants, CATALOGUE.values(), strict=True):
        params = variant["properties"]["params"]
        names = [p.name for p in spec.params]
        assert variant["properties"]["type"]["enum"] == [spec.type.value]
        assert (list(params["properties"]), params["required"]) == (names, names)
        assert params["additionalProperties"] is False
        for p in spec.params:
            if p.choices:
                assert params["properties"][p.name]["enum"] == list(p.choices)


def test_a_type_with_a_fixed_kind_is_offered_only_that_kind():
    """Only a custom rule leaves cleansing-or-validation to the model."""
    variants = suggestions_schema()["properties"]["suggestions"]["items"]["anyOf"]
    kinds = {v["properties"]["type"]["enum"][0]: v["properties"]["kind"]["enum"] for v in variants}

    assert kinds["expected_window"] == ["validation"]
    assert kinds["dedupe_policy"] == ["cleansing"]
    assert kinds["custom"] == ["cleansing", "validation"]


def test_the_schemas_carry_a_title_langchains_structured_output_needs():
    """LangChain's structured-output helper needs a name for a raw JSON Schema before it will send it."""
    assert patterns_schema()["title"] == "patterns"
    assert suggestions_schema()["title"] == "suggestions"


def test_the_suggestion_prompt_carries_the_catalogue_and_the_rules_verification_applies():
    """Every catalogue type is offered, and the no-new-numbers rule is stated in the system prompt.

    Verification checks only the schema now, so the prompt is the one place that rule exists.
    """
    prompt = suggestions_prompt(_pack(), [])

    for t in SuggestionType:
        assert f'"type":"{t.value}"' in prompt
    assert "Do not compute new numbers" in SUGGESTIONS_SYSTEM
