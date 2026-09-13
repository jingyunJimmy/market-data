"""Intelligent-insights orchestration.

    bars ─▶ QualityValidator ─▶ build_evidence_pack ─▶ identify_patterns ─▶ verify_patterns
                                                     ─▶ suggest_rules     ─▶ verify_suggestions

A client that cannot answer at all raises ``InsightProviderError``, which is
left to propagate: there is no answer to show, and the route reports why.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from collections.abc import Callable, Sequence

from market_data.config import Settings, get_settings
from market_data.domain.insights import (
    EvidencePack,
    InsightsReport,
    build_evidence_pack,
    verify_patterns,
    verify_suggestions,
)
from market_data.domain.insights.models import Pattern
from market_data.domain.models import Frequency
from market_data.domain.quality import QualityValidator
from market_data.llm.claude import ClaudeClient, InsightProviderError
from market_data.llm.prompts import (
    PATTERNS_SYSTEM,
    SUGGESTIONS_SYSTEM,
    patterns_prompt,
    patterns_schema,
    suggestions_prompt,
    suggestions_schema,
)
from market_data.storage.repository import BarRepository

logger = logging.getLogger(__name__)


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class InsightsService:
    def __init__(
        self,
        repo: BarRepository,
        *,
        client: ClaudeClient,
        settings: Settings | None = None,
        clock: Callable[[], dt.datetime] = _utc_now,
    ) -> None:
        self._repo = repo
        self._client = client
        self._settings = settings or get_settings()
        self._clock = clock

    def generate(
        self,
        *,
        contract: str | None = None,
        frequency: Frequency = Frequency.MINUTE,
        start: dt.date | None = None,
        end: dt.date | None = None,
    ) -> InsightsReport:
        started = time.monotonic()
        logger.info(
            "Generating insights: contract=%s frequency=%s range=%s..%s",
            contract,
            frequency.value,
            start or "first",
            end or "last",
        )
        pack = self.evidence(contract=contract, frequency=frequency, start=start, end=end)
        logger.info(
            "Evidence pack built: %d findings over %s bars, %d trading days",
            len(pack.evidence),
            f"{pack.scope.bars_checked:,}",
            pack.scope.trading_days,
        )
        logger.info("Step 1/2: identifying patterns")
        patterns, rejected = verify_patterns(self._identify_patterns(pack))
        logger.info("Step 1/2 done: %d patterns kept, %d rejected", len(patterns), len(rejected))
        logger.info("Step 2/2: suggesting rules")
        suggestions, dropped = verify_suggestions(self._suggest_rules(pack, patterns))
        logger.info("Step 2/2 done: %d suggestions kept, %d rejected", len(suggestions), len(dropped))
        logger.info("Insights ready for %s in %.1fs", contract, time.monotonic() - started)
        return InsightsReport(
            scope=pack.scope,
            model=self._client.model,
            generated_at=self._clock(),
            evidence=pack.evidence,
            patterns=patterns,
            suggestions=suggestions,
            rejected=[*rejected, *dropped],
        )

    def evidence(
        self,
        *,
        contract: str | None = None,
        frequency: Frequency = Frequency.MINUTE,
        start: dt.date | None = None,
        end: dt.date | None = None,
    ) -> EvidencePack:
        """The evidence pack for one filter, which is all the model ever sees.

        The validator runs here rather than through ``QualityService.report``
        for two reasons: the bars are needed afterwards to count trading days,
        and the detail cap has to be the insights one. The dashboard's report
        samples a page of occurrences per finding; a distribution needs them all.
        """
        bars = self._repo.load_bars(contract=contract, frequency=frequency, start=start, end=end)
        superseded = self._repo.load_superseded(contract=contract, frequency=frequency, start=start, end=end)
        settings = self._settings.model_copy(
            update={"issue_detail_limit": self._settings.insights_max_occurrences}
        )
        report = QualityValidator(settings=settings).validate(
            bars,
            contract=contract,
            frequency=frequency,
            start=start,
            end=end,
            superseded=superseded,
        )
        trading_days = int(bars["trading_date"].n_unique()) if bars.height else 0
        return build_evidence_pack(report, trading_days=trading_days, settings=self._settings)

    def _identify_patterns(self, pack: EvidencePack) -> list[object]:
        if not pack.evidence:
            # Nothing to read; not worth a round trip.
            logger.info("No quality findings in range; skipping the Claude call")
            return []
        answer = self._client.complete_json(
            system=PATTERNS_SYSTEM, user=patterns_prompt(pack), schema=patterns_schema()
        )
        return _items(answer, "patterns")

    def _suggest_rules(self, pack: EvidencePack, patterns: Sequence[Pattern]) -> list[object]:
        if not patterns:
            logger.info("No patterns to build on; skipping the Claude call")
            return []
        answer = self._client.complete_json(
            system=SUGGESTIONS_SYSTEM, user=suggestions_prompt(pack, patterns), schema=suggestions_schema()
        )
        return _items(answer, "suggestions")


def _items(answer: object, key: str) -> list[object]:
    """The list under ``key``. Its items are left for verification to judge one by one."""
    items = answer.get(key) if isinstance(answer, dict) else None
    if not isinstance(items, list):
        raise InsightProviderError(f"LLM answer has no {key!r} list")
    return items
