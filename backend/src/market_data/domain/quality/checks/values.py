"""Invalid price / volume checks.

Everything here is a *single-row* predicate: a bar is judged on its own fields,
without reference to its neighbours or to the time axis. That is what separates
this module from its siblings -- `gaps.py` looks at the spacing between bars,
`duplicates.py` at bars sharing an instant, `outliers.py` at a bar relative to
the distribution around it. If a check needs to look at anything but the row in
front of it, it does not belong in this file.

**Where the ingestion boundary falls.** Normalisation rejects only what cannot
form a bar. A null in any of open/high/low/close is `missing_ohlc` and the row
never reaches storage, so there is deliberately no missing-price check here --
it would be unreachable. Volume has no such rejection branch: a bar without
volume is still a bar, and the null survives to the database as SQL NULL. That
asymmetry is the whole reason `missing_volume` exists below. Rows that are
"suspicious but usable" are ingestion's to keep and ours to report.

**Severity is context aware.** The same predicate means different things at
different frequencies, because a daily bar is not a downsampled minute bar --
it carries exchange conventions a minute bar does not:

    check                 daily      minute
    open_out_of_range     WARNING    ERROR
    close_out_of_range    INFO       ERROR
    missing_volume        --         WARNING

A daily ``close`` outside the day's ``[low, high]`` is normal settlement
behaviour and is only informational, whereas the same thing on a minute bar is
a real error.
"""

from __future__ import annotations

from collections.abc import Iterable

import polars as pl

from market_data.domain.models import Frequency, IssueCategory, QualityIssue, Severity
from market_data.domain.quality.details import bar_details, detail_fields
from market_data.domain.quality.rules import RuleContext

_OHLC = ("open", "high", "low", "close")


def _issue(
    ctx: RuleContext, contract: str, code: str, sev: Severity, mask: pl.Expr, msg: str
) -> QualityIssue | None:
    """Turn one predicate into at most one summary issue, or None if it is clean.

    The single place a `QualityIssue` is built in this module, so the shape of
    what reaches the dashboard is decided once rather than per check.

    Known weaknesses, neither of them load-bearing today:

    * It re-filters the whole of ``ctx.bars`` per call, so a contract running
      seven checks costs seven full scans. The caller already holds the
      per-contract group and could pass it in.
    * ``example_ts`` is the first row in *frame order* while ``start_ts`` is
      ``min()``. On an unsorted frame the example need not be the earliest hit.
    """
    hits = ctx.bars.filter((pl.col("contract") == contract) & mask)
    if hits.is_empty():
        return None
    # The evidence for "high < low" is the bar's own OHLCV: seeing the numbers
    # is the whole point, and they are already sitting in `hits`.
    limit = ctx.settings.issue_detail_limit
    return QualityIssue(
        category=IssueCategory.INVALID_VALUE,
        code=code,
        severity=sev,
        contract=contract,
        frequency=ctx.frequency,
        message=f"{hits.height} bar(s): {msg}",
        count=hits.height,
        start_ts=hits["ts"].min(),
        end_ts=hits["ts"].max(),
        context={"example_ts": str(hits["ts"][0])},
        **detail_fields(bar_details(hits, limit, columns=(*_OHLC, "volume")), limit, total=hits.height),
    )


def invalid_values(ctx: RuleContext) -> Iterable[QualityIssue]:
    """Every single-row validity check, one summary issue per contract per code.

    The checks are data, not control flow: each is a
    ``(code, severity, predicate, message)`` tuple, so adding one is adding a
    row to the list below. Note that the predicates are not mutually exclusive
    -- a bar with ``high < low`` will usually trip the two range checks as well,
    and is reported by each.
    """
    if ctx.bars.is_empty():
        return []
    out: list[QualityIssue] = []
    daily = ctx.frequency is Frequency.DAILY

    for contract, _grp in ctx.bars.group_by("contract"):
        name = str(contract[0] if isinstance(contract, tuple) else contract)
        checks: list[tuple[str, Severity, pl.Expr, str]] = [
            # Structurally impossible at any frequency: the day's extremes are
            # inverted, which means a field swap or a bad upstream join, never
            # a market event.
            ("high_lt_low", Severity.ERROR, pl.col("high") < pl.col("low"), "high < low"),
            (
                # A minute bar's open is by definition its first trade, so it
                # cannot sit outside the minute's own range -- that is an error.
                # A daily open may come from an auction or a different session
                # convention, so it only earns a warning.
                "open_out_of_range",
                Severity.WARNING if daily else Severity.ERROR,
                (pl.col("open") > pl.col("high")) | (pl.col("open") < pl.col("low")),
                "open outside [low, high]",
            ),
            (
                # The clearest case of context: a daily futures "close" is
                # usually the exchange settlement price, computed from a closing
                # window or a fair-value model, and legitimately lands outside
                # the traded range. Informational on daily, an error on minute.
                "close_out_of_range",
                Severity.INFO if daily else Severity.ERROR,
                (pl.col("close") > pl.col("high")) | (pl.col("close") < pl.col("low")),
                "close outside [low, high]" + (" (settlement prints legitimately do this)" if daily else ""),
            ),
            (
                # min_horizontal <= 0 catches any of the four. Zero is the
                # fingerprint of a placeholder or a null filled in upstream;
                # a negative price is a sign flip.
                "non_positive_price",
                Severity.ERROR,
                pl.min_horizontal(*_OHLC) <= 0,
                "price <= 0",
            ),
            (
                # inf/nan pass Pydantic's float validation but poison every
                # downstream return, VWAP and aggregate, so they are caught here
                # rather than being discovered in a chart.
                "non_finite_price",
                Severity.ERROR,
                pl.any_horizontal(pl.col(c).is_infinite() | pl.col(c).is_nan() for c in _OHLC),
                "non-finite price (inf/nan)",
            ),
            (
                # Volume is a count; a negative one has no reading. Null volume
                # makes this comparison null and the filter drops it, so this
                # never double-reports a bar caught by `missing_volume`.
                "negative_volume",
                Severity.ERROR,
                pl.col("volume") < 0,
                "negative volume",
            ),
        ]
        if ctx.frequency is Frequency.MINUTE:
            # Minute-only: a minute bar exists because trades were seen in that
            # minute, so a missing volume means the field was lost in assembly.
            # Daily bars reach us from a separate source whose empty-volume
            # semantics differ, so the same predicate there would be noise.
            checks.append(
                (
                    "missing_volume",
                    Severity.WARNING,
                    pl.col("volume").is_null(),
                    "minute bar with no volume",
                )
            )

        for code, sev, expr, msg in checks:
            issue = _issue(ctx, name, code, sev, expr, msg)
            if issue is not None:
                out.append(issue)
    return out
