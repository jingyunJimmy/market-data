"""InsightsService's own round trip with the LLM: what it skips, and how an
answer that doesn't match the schema becomes ``InsightProviderError`` rather
than a silent empty result.

No repository is touched here -- ``_identify_patterns`` and ``_suggest_rules``
read only the evidence pack and the client, so a fake client is enough.
"""

from __future__ import annotations

import pytest

from market_data.domain.insights.models import EvidencePack, InsightScope
from market_data.domain.models import Frequency
from market_data.llm import InsightProviderError
from market_data.services.insights_service import InsightsService


class _FakeClient:
    model = "fake"

    def __init__(self, *answers: object) -> None:
        self._answers = list(answers)
        self.schema_titles: list[str] = []

    def complete_json(self, *, system: str, user: str, schema: dict) -> object:
        self.schema_titles.append(schema["title"])
        return self._answers.pop(0)


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


def _service(client: _FakeClient) -> InsightsService:
    return InsightsService(repo=None, client=client)  # type: ignore[arg-type]


def test_an_answer_without_the_expected_list_is_a_provider_error():
    """An answer under the wrong key is no answer, so it raises rather than reading as zero patterns."""
    client = _FakeClient({"findings": []})

    with pytest.raises(InsightProviderError, match="'patterns'"):
        _service(client)._identify_patterns(_pack())


def test_an_empty_pack_is_not_worth_a_round_trip():
    """No evidence means no pattern call, and no patterns means no suggestion call."""
    client = _FakeClient()

    assert _service(client)._identify_patterns(_pack(evidence=[])) == []
    assert _service(client)._suggest_rules(_pack(), []) == []
    assert client.schema_titles == []
