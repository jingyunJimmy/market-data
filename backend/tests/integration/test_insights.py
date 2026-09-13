"""Intelligent insights end to end: real bars, the real validator, the real route.

The fixture plants one unmistakable regularity -- the daily 16:00-17:00 CT
halt, missing from every session. No model is called: a scripted client
stands in for the LLM and answers the way a model would, reading the evidence
pack out of the prompt it was sent. Verification no longer checks a pattern
or a suggestion against the evidence it cites, only that each one is
well-formed JSON, so the scripted model's invented pattern and its suggestion
that does not follow from any evidence are both kept -- only a suggestion
with a malformed type is refused. That keeps the test about the pipeline --
what the model is shown, and what survives verification -- rather than about
any particular model's judgement.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import anthropic
import httpx2 as httpx
import polars as pl
import pytest

from market_data.config import Settings
from market_data.domain.models import Frequency
from market_data.ingestion import IngestionService
from market_data.llm import ClaudeClient, InsightProviderError
from market_data.services import InsightsService
from market_data.synthetic import clean_minute_bars

pytestmark = pytest.mark.integration


def _halted_week() -> pl.DataFrame:
    """Five sessions of 13:00-17:59 CT minute bars with the 16:00 hour missing from each."""
    days = [
        clean_minute_bars(start=dt.datetime(2024, 6, 3 + d, 13, 0), minutes=300, seed=d + 1) for d in range(5)
    ]
    return pl.concat(days).filter(pl.col("timestamp_chicago_wall").dt.hour() != 16)


def _section(user: str, label: str) -> Any:
    """The JSON that ``prompts.py`` embedded under one ``label:`` heading."""
    return json.loads(user.split(f"{label}:\n", 1)[1].split("\n\n", 1)[0])


class _ScriptedModel:
    """Answers from the evidence it is shown: right about the halt, inventing a trend."""

    model = "scripted"

    def __init__(self) -> None:
        self.offered: list[str] = []

    def complete_json(self, *, system: str, user: str, schema: dict) -> dict:
        if schema["title"] == "patterns":
            return {"patterns": self._patterns(_section(user, "Evidence pack"))}
        patterns = _section(user, "Verified patterns")
        self.offered = [p["id"] for p in patterns]
        return {"suggestions": self._suggestions()}

    def _patterns(self, pack: dict) -> list[dict]:
        gaps = next((e for e in pack["evidence"] if e["code"] == "intra_session_gap"), None)
        if gaps is None:
            return []
        top = gaps["top_hours"][0]
        base = {
            "classification": "expected_market_behavior",
            "contracts": [gaps["contract"]],
            "evidence_refs": [gaps["id"]],
            "confidence": "high",
        }
        return [
            {
                **base,
                "id": "P1",
                "title": f"Gaps recur at {top['hour']:02d}:00 CT",
                "explanation": (
                    f"{top['share']:.0%} of {gaps['analysed']} gaps start in the "
                    f"{top['hour']:02d}:00 CT hour, on {gaps['days_affected']} of "
                    f"{pack['scope']['trading_days']} trading days."
                ),
            },
            {**base, "id": "P2", "title": "Gaps are growing", "explanation": "Gaps rose 340% week on week."},
        ]

    def _suggestions(self) -> list[dict]:
        return [
            {
                "pattern_id": "P1",
                "type": "expected_window",
                "kind": "validation",
                "params": {
                    "contract": "CL_TEST",
                    "code": "intra_session_gap",
                    "start_ct": "16:00",
                    "end_ct": "17:00",
                },
                "rationale": "The 16:00-17:00 CT halt is scheduled.",
            },
            {
                "pattern_id": "P1",
                "type": "adjust_threshold",
                "kind": "validation",
                "params": {"setting": "outlier_mad_threshold", "value": 12},
                "rationale": "Loosen the outlier check, which this pattern's evidence says nothing about.",
            },
            {
                "pattern_id": "P1",
                "type": "not_a_real_type",
                "kind": "validation",
                "params": {},
                "rationale": "A malformed type, to show the schema check still catches something.",
            },
        ]


class _RaisingStructured:
    def invoke(self, messages: list) -> object:
        raise anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com"))


class _UnreachableLlm:
    def with_structured_output(self, schema: dict, *, method: str, include_raw: bool) -> _RaisingStructured:
        return _RaisingStructured()


def _unreachable_model() -> ClaudeClient:
    return ClaudeClient(model="claude-sonnet-5", llm=_UnreachableLlm())


@pytest.fixture
def halted_repo(repo):
    IngestionService(repo).ingest_frame(_halted_week(), source="halted", frequency=Frequency.MINUTE)
    return repo


# --------------------------------------------------------------------------- #
# The service
# --------------------------------------------------------------------------- #


def test_the_model_is_shown_the_halt(halted_repo):
    """Whether a model can find the halt depends on whether the evidence shows it."""
    pack = InsightsService(halted_repo, client=_ScriptedModel()).evidence(
        contract="CL_TEST", frequency=Frequency.MINUTE
    )

    gaps = next(e for e in pack.evidence if e.code == "intra_session_gap")
    assert (gaps.top_hours[0].hour, gaps.top_hours[0].share) == (16, 1.0)
    assert (gaps.days_affected, pack.scope.trading_days) == (5, 5)
    assert gaps.median_duration_min == 61.0


def test_the_evidence_reads_every_occurrence_not_the_dashboard_sample(halted_repo):
    """The report the dashboard shows caps each finding's list; a distribution needs all of it."""
    capped = Settings(issue_detail_limit=2)
    pack = InsightsService(halted_repo, client=_ScriptedModel(), settings=capped).evidence(
        contract="CL_TEST", frequency=Frequency.MINUTE
    )

    gaps = next(e for e in pack.evidence if e.code == "intra_session_gap")
    assert gaps.analysed == gaps.occurrences == 5


def test_a_model_answer_is_verified_item_by_item_and_the_refusals_are_reported(halted_repo):
    """The whole generate path: the patterns kept are what the suggestion call is shown.

    A malformed item is reported in ``rejected`` with its reason rather than dropped silently.
    """
    model = _ScriptedModel()
    report = InsightsService(halted_repo, client=model).generate(
        contract="CL_TEST", frequency=Frequency.MINUTE
    )

    # Neither patterns nor suggestions are checked for grounding any more, so
    # both patterns and both well-formed suggestions survive.
    assert [p.id for p in report.patterns] == ["P1", "P2"]
    assert [s.type for s in report.suggestions] == ["expected_window", "adjust_threshold"]
    assert model.offered == ["P1", "P2"]
    reasons = {r.item: r.reason for r in report.rejected}
    assert "type" in reasons["not_a_real_type"]
    assert report.model == "scripted"


def test_a_model_that_cannot_answer_raises_rather_than_returning_nothing(halted_repo):
    """An empty report would read as "no patterns", which is a claim nobody made."""
    with pytest.raises(InsightProviderError, match="unreachable"):
        InsightsService(halted_repo, client=_unreachable_model()).generate(
            contract="CL_TEST", frequency=Frequency.MINUTE
        )


# --------------------------------------------------------------------------- #
# The route
# --------------------------------------------------------------------------- #


def _serve(api_client, client) -> None:
    from market_data.api.deps import get_insights_service, get_repo

    api_client.app.dependency_overrides[get_insights_service] = lambda: InsightsService(
        get_repo(), client=client
    )


@pytest.fixture
def halted_api(api_client):
    from market_data.api.deps import get_repo

    IngestionService(get_repo()).ingest_frame(_halted_week(), source="halted", frequency=Frequency.MINUTE)
    return api_client


def test_post_insights_returns_patterns_suggestions_and_their_evidence(halted_api):
    """The route returns the whole report: patterns, the evidence rows they cite, and the rejections."""
    _serve(halted_api, _ScriptedModel())

    r = halted_api.post("/insights", params={"contract": "CL_TEST", "frequency": "minute"})

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["model"] == "scripted"
    assert [p["title"] for p in body["patterns"]] == ["Gaps recur at 16:00 CT", "Gaps are growing"]
    # The evidence travels with the patterns, so the client renders figures from it.
    cited = {ref for p in body["patterns"] for ref in p["evidence_refs"]}
    assert cited <= {e["id"] for e in body["evidence"]}
    assert {r["item"] for r in body["rejected"]} == {"not_a_real_type"}


def test_an_unreachable_model_is_a_503_that_says_why(halted_api):
    """Not a 500: the application is fine, and the reader needs to know what to fix."""
    _serve(halted_api, _unreachable_model())

    r = halted_api.post("/insights", params={"contract": "CL_TEST"})

    assert r.status_code == 503
    assert "unreachable" in r.json()["detail"]


def test_insights_are_not_served_on_get(halted_api):
    """Generating is work with a cost; it must never happen as a side effect of a read."""
    assert halted_api.get("/insights", params={"contract": "CL_TEST"}).status_code == 405


def test_insights_require_a_contract(halted_api):
    """No contract is a 422, so a run across every stored contract cannot be started by accident."""
    assert halted_api.post("/insights").status_code == 422


def test_an_empty_range_returns_no_patterns_without_calling_the_model(halted_api):
    """Nothing to read is not worth a round trip -- the unreachable model is never touched."""
    _serve(halted_api, _unreachable_model())

    r = halted_api.post(
        "/insights", params={"contract": "CL_TEST", "start": "2030-01-01", "end": "2030-01-31"}
    )

    assert r.status_code == 200, r.text
    assert r.json()["patterns"] == [] and r.json()["scope"]["bars_checked"] == 0
