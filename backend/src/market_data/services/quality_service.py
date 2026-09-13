"""Data-quality orchestration."""

from __future__ import annotations

import datetime as dt

from market_data.config import Settings, get_settings
from market_data.domain.models import (
    Frequency,
    IssueDetailPage,
    MissingTimestampPage,
    QualityReport,
)
from market_data.domain.quality import QualityValidator
from market_data.domain.quality.missing import missing_timestamps
from market_data.storage.repository import BarRepository


class QualityService:
    def __init__(
        self,
        repo: BarRepository,
        *,
        validator: QualityValidator | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._repo = repo
        self._settings = settings or get_settings()
        self._validator = validator or QualityValidator(settings=self._settings)

    def report(
        self,
        *,
        contract: str | None = None,
        frequency: Frequency = Frequency.MINUTE,
        start: dt.date | None = None,
        end: dt.date | None = None,
    ) -> QualityReport:
        bars = self._repo.load_bars(contract=contract, frequency=frequency, start=start, end=end)
        # Instant conflicts are resolved at ingest, so they are not in `bars`.
        # Load what was displaced too, or resolving them would hide them.
        superseded = self._repo.load_superseded(contract=contract, frequency=frequency, start=start, end=end)
        return self._validator.validate(
            bars,
            contract=contract,
            frequency=frequency,
            start=start,
            end=end,
            superseded=superseded,
        )

    def missing_timestamps(
        self,
        *,
        contract: str | None = None,
        frequency: Frequency = Frequency.MINUTE,
        start: dt.date | None = None,
        end: dt.date | None = None,
        limit: int = 2000,
        offset: int = 0,
    ) -> MissingTimestampPage:
        """The flat list of instants with no bar, for the same filter as `report`.

        Kept off `report` on purpose: the gaps in a report number in the
        thousands, the instants inside them in the hundreds of thousands. One
        response should not carry both.
        """
        bars = self._repo.load_bars(contract=contract, frequency=frequency, start=start, end=end)
        return missing_timestamps(
            bars,
            contract=contract,
            frequency=frequency,
            settings=self._settings,
            limit=limit,
            offset=offset,
        )

    def issue_details(
        self,
        *,
        code: str,
        contract: str | None = None,
        frequency: Frequency = Frequency.MINUTE,
        start: dt.date | None = None,
        end: dt.date | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> IssueDetailPage:
        """One finding's evidence, paged past the sample the report carries.

        The rules cap their own evidence at ``issue_detail_limit`` so a report
        stays a reasonable size. To page deeper, the check is re-run against a
        settings copy whose cap covers the requested window, and the page is
        sliced out of the result.

        Re-running rather than caching: a rule is a pure function of the bars,
        the request is already filtered to one contract and range, and the
        alternative is holding per-session report state on the server. The
        counts involved are bar-level (thousands), not instant-level -- that is
        what ``missing_timestamps`` handles separately.

        Several issues can share a ``code`` (``exact_duplicate`` raises one per
        repeated hash), so their evidence is concatenated in report order and
        their totals summed. For every other rule this is a list of one.
        """
        needed = max(offset + limit, 1)
        settings = self._settings.model_copy(update={"issue_detail_limit": needed})
        report = QualityService(
            self._repo, validator=QualityValidator(settings=settings), settings=settings
        ).report(contract=contract, frequency=frequency, start=start, end=end)

        matching = [i for i in report.issues if i.code == code]
        details = [d for issue in matching for d in issue.details]
        return IssueDetailPage(
            contract=contract,
            frequency=frequency,
            code=code,
            total=sum(i.detail_total for i in matching),
            offset=offset,
            details=details[offset : offset + limit],
        )
