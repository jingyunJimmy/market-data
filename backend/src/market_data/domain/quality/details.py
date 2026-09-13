"""Turning a rule's findings into per-occurrence evidence for the dashboard.

Every check produces a summary (`"9594 intra-session gaps"`). That is enough to
raise an eyebrow and not enough to act on, so each rule can also enumerate the
individual occurrences behind its summary as :class:`IssueDetail` rows.

Two concerns are shared by every rule, which is why they live here rather than
being re-implemented per check:

* **Capping.** Occurrence counts are unbounded. One thin contract yields
  thousands of gaps, and an invalid-value rule could in principle flag every
  bar in the range. :func:`detail_fields` truncates to
  ``Settings.issue_detail_limit`` and reports the true total alongside, so the
  client never has to guess whether it is seeing everything.
* **Consistency.** ``details``, ``detail_total`` and ``detail_truncated`` only
  mean something together. Building them in one place stops a rule from
  shipping a truncated list that claims to be complete.

Adding evidence to a rule is therefore a two-line change: build the
:class:`IssueDetail` list, then splat ``detail_fields(...)`` into the
:class:`QualityIssue` constructor.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence

import polars as pl

from market_data.domain.models import IssueDetail

#: The bar's own numbers, attached to every piece of evidence that points at a
#: real row. A finding is far easier to judge next to the OHLCV that produced it
#: -- a return of 0.11 could be a bad print or a genuine move, and only the
#: prices say which -- so these trail the check's own figures in every detail
#: table rather than each rule choosing for itself.
BAR_VALUE_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close", "volume")


def detail_fields(
    details: Sequence[IssueDetail], limit: int, *, total: int | None = None
) -> dict[str, object]:
    """The three ``QualityIssue`` evidence fields, kept in sync.

    Splat the result into the constructor::

        QualityIssue(..., **detail_fields(rows, ctx.settings.issue_detail_limit))

    ``details`` is expected to be in the order the user should read it, which
    for a time series means chronological: truncation keeps the *first* rows,
    so an arbitrary order would silently pick an arbitrary sample.

    Pass ``total`` when the caller already sliced its source down to ``limit``
    rather than materialising every occurrence. A rule can flag six figures of
    bars, and building that many models only to discard all but the first few
    hundred is wasted work -- but the count it reports must still be the real
    one, which is what this argument is for.
    """
    kept = list(details[:limit])
    real_total = max(total if total is not None else len(details), len(kept))
    return {
        "details": kept,
        "detail_total": real_total,
        "detail_truncated": real_total > len(kept),
    }


def date_detail(day: dt.date, **values: float | int | str | None) -> IssueDetail:
    """Evidence for a rule whose unit is a *session*, not an instant.

    A daily bar is stamped at UTC midnight, so the timestamp round-trips, but
    rendering it as a timestamp would put a meaningless ``00:00`` in front of
    the user. The ISO date goes in ``label`` and the dashboard prefers it.
    """
    return IssueDetail(
        ts=dt.datetime.combine(day, dt.time(), tzinfo=dt.UTC),
        label=day.isoformat(),
        values={k: v for k, v in values.items() if v is not None},
    )


def bar_details(
    hits: pl.DataFrame,
    limit: int,
    *,
    columns: Sequence[str],
    ts_col: str = "ts",
    end_col: str | None = None,
) -> list[IssueDetail]:
    """Evidence rows for a check that flags *existing* bars.

    The gap rules are the odd ones out: an absence has no row to point at, so
    they synthesise their evidence. Every other check already holds a frame of
    the offending bars, and this turns it into details directly.

    ``columns`` names what goes into ``values`` and, because the dashboard
    renders one column per key, is effectively the detail table's schema. Round
    or rename in the frame beforehand: the key is used verbatim as the heading.

    Sorted by ``ts_col`` then sliced, because truncation keeps the first rows --
    an unsorted frame would ship an arbitrary sample of an ordered series.
    """
    if hits.is_empty():
        return []
    wanted = [ts_col, *([end_col] if end_col else []), *columns]
    rows = hits.select(wanted).sort(ts_col).head(limit)
    return [
        IssueDetail(
            ts=row[ts_col],
            end_ts=row[end_col] if end_col else None,
            values={c: row[c] for c in columns},
        )
        for row in rows.iter_rows(named=True)
    ]
