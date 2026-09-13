"""Duplicate and conflicting-record detection."""

from __future__ import annotations

from collections.abc import Iterable

import polars as pl

from market_data.domain.models import IssueCategory, IssueDetail, QualityIssue, Severity
from market_data.domain.quality.details import BAR_VALUE_COLUMNS, bar_details, detail_fields
from market_data.domain.quality.rules import RuleContext


def _with_bar_values(frame: pl.DataFrame, bars: pl.DataFrame, *, on: str | list[str]) -> pl.DataFrame:
    """Attach one representative bar's OHLCV to each row of an aggregate frame.

    These rules group before they report, so the aggregate has lost the numbers.
    A left join on the grouping key brings them back.

    "Representative" is exact for the byte-identical and identical-value cases,
    where every copy carries the same numbers. For a *conflicting* instant it is
    the first variant only, which is why the `variants` count stays on the row:
    it tells the reader these prices are one of several claims, not the value.
    """
    keys = [on] if isinstance(on, str) else on
    first = bars.unique(subset=keys, keep="first", maintain_order=True)
    return frame.join(first.select(*keys, *BAR_VALUE_COLUMNS), on=keys, how="left")


def exact_duplicates(ctx: RuleContext) -> Iterable[QualityIssue]:
    """Byte-identical rows (same ``row_hash``) appearing more than once.

    The store's primary key collapses these on ingest, so this mostly matters
    for validating a raw frame before persistence.
    """
    if ctx.bars.is_empty() or "row_hash" not in ctx.bars.columns:
        return []
    dups = (
        ctx.bars.group_by("contract", "row_hash")
        .agg(pl.len().alias("n"), pl.col("ts").min().alias("start"), pl.col("ts").max().alias("end"))
        .filter(pl.col("n") > 1)
    )
    limit = ctx.settings.issue_detail_limit
    # Every copy is byte-identical, so any one of them carries the shared values.
    bars_by_hash = _with_bar_values(dups, ctx.bars, on="row_hash")
    out: list[QualityIssue] = []
    for contract, row_hash, n, start, end, *bar in bars_by_hash.iter_rows():
        # One issue per repeated hash, so its evidence is the single span those
        # copies occupy. The hash is included because it is the handle for
        # finding the rows again in `bars`.
        detail = IssueDetail(
            ts=start,
            end_ts=end if end != start else None,
            values={
                "row_hash": row_hash,
                "copies": int(n),
                **dict(zip(BAR_VALUE_COLUMNS, bar, strict=True)),
            },
        )
        out.append(
            QualityIssue(
                category=IssueCategory.DUPLICATE,
                code="exact_duplicate",
                severity=Severity.ERROR,
                contract=contract,
                frequency=ctx.frequency,
                message=f"{n} byte-identical rows for {contract}",
                count=int(n) - 1,
                start_ts=start,
                end_ts=end,
                **detail_fields([detail], limit),
            )
        )
    return out


def conflicting_duplicates(ctx: RuleContext) -> Iterable[QualityIssue]:
    """Multiple rows for the same ``(contract, ts)`` with *different* values."""
    if ctx.bars.is_empty():
        return []
    grouped = ctx.bars.group_by("contract", "ts").agg(
        pl.struct("open", "high", "low", "close", "volume").n_unique().alias("variants"),
        pl.len().alias("rows"),
    )
    conflicts = grouped.filter(pl.col("rows") > 1)
    limit = ctx.settings.issue_detail_limit
    out: list[QualityIssue] = []
    for contract, grp in conflicts.group_by("contract"):
        name = contract[0] if isinstance(contract, tuple) else contract
        real = grp.filter(pl.col("variants") > 1)
        if real.height:
            out.append(
                QualityIssue(
                    category=IssueCategory.CONFLICT,
                    code="conflicting_duplicate",
                    severity=Severity.ERROR,
                    contract=str(name),
                    frequency=ctx.frequency,
                    message=(f"{real.height} timestamps with conflicting OHLCV values for {name}"),
                    count=int(real.height),
                    start_ts=real["ts"].min(),
                    end_ts=real["ts"].max(),
                    context={"example_ts": str(real["ts"][0])},
                    # `variants` is how many *different* OHLCV tuples claim this
                    # instant, `rows` how many records in total -- together they
                    # say whether it is one disagreement or a pile of them.
                    **detail_fields(
                        bar_details(
                            _with_bar_values(real, ctx.bars, on=["contract", "ts"]),
                            limit,
                            columns=("variants", "rows", *BAR_VALUE_COLUMNS),
                        ),
                        limit,
                        total=real.height,
                    ),
                )
            )
        benign = grp.filter(pl.col("variants") == 1)
        if benign.height:
            out.append(
                QualityIssue(
                    category=IssueCategory.DUPLICATE,
                    code="repeated_timestamp",
                    severity=Severity.WARNING,
                    contract=str(name),
                    frequency=ctx.frequency,
                    message=f"{benign.height} timestamps repeated with identical values for {name}",
                    count=int(benign.height),
                    start_ts=benign["ts"].min(),
                    end_ts=benign["ts"].max(),
                    **detail_fields(
                        bar_details(
                            _with_bar_values(benign, ctx.bars, on=["contract", "ts"]),
                            limit,
                            columns=("rows", *BAR_VALUE_COLUMNS),
                        ),
                        limit,
                        total=benign.height,
                    ),
                )
            )
    return out


def resolved_instant_conflicts(ctx: RuleContext) -> Iterable[QualityIssue]:
    """Instants where the source disagreed with itself and ingestion picked one.

    Ingestion enforces one bar per ``(contract, frequency, ts)`` by keeping the
    latest row, so these conflicts are no longer visible in ``bars``. Reporting
    them from ``superseded_bars`` is what stops "resolved" from meaning
    "silently hidden": the analytics are clean *and* the user still learns the
    source was inconsistent.

    WARNING rather than ERROR: the data is now self-consistent, and the audit
    trail holds every discarded row. It is a fact about the feed, not a defect
    in the store.

    Counted by distinct instant, not by row, so re-ingesting the same file does
    not inflate the figure -- each run appends its own audit rows.
    """
    if ctx.superseded.is_empty():
        return []
    limit = ctx.settings.issue_detail_limit
    out: list[QualityIssue] = []
    for contract, grp in ctx.superseded.group_by("contract", maintain_order=True):
        name = str(contract[0] if isinstance(contract, tuple) else contract)
        instants = grp.select("ts").unique()
        out.append(
            QualityIssue(
                category=IssueCategory.CONFLICT,
                code="resolved_instant_conflict",
                severity=Severity.WARNING,
                contract=name,
                frequency=ctx.frequency,
                message=(
                    f"{instants.height} timestamps had conflicting OHLCV values for {name}; "
                    "the latest row was kept and the rest are in superseded_bars"
                ),
                count=int(instants.height),
                start_ts=instants["ts"].min(),
                end_ts=instants["ts"].max(),
                context={
                    "discarded_rows": int(grp.height),
                    "example_ts": str(instants["ts"].min()),
                    "reasons": sorted(set(grp["reason"].to_list())),
                },
                # The evidence here is the *discarded* row's own OHLCV. The
                # first question about a bar that lost a tie-break is what it
                # actually said, and `superseded_bars` is the only place that
                # survives -- so this is the one drill-down that shows data no
                # other view in the dashboard can reach.
                **detail_fields(
                    bar_details(
                        grp,
                        limit,
                        # The *discarded* row's own numbers, not the survivor's:
                        # `grp` is a slice of superseded_bars.
                        columns=("reason", *BAR_VALUE_COLUMNS),
                    ),
                    limit,
                    total=grp.height,
                ),
            )
        )
    return out
