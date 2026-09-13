"""Statistical outlier detection (optional signal, WARNING/INFO severity).

Both checks here answer the same question -- "is this bar far from the bars
around it?" -- and both answer it with a *robust* estimator, i.e. one whose
value is not moved by the very points it is meant to find.

**Why not mean / standard deviation.** The classic z-score divides by the
sample standard deviation, and the standard deviation is computed from the
squared distance of every point to the mean. A single print 500x too large
therefore inflates the denominator it is being tested against, widening the
threshold until the outlier looks ordinary and the merely-bad prints beside it
are hidden completely. That is the masking effect, and on a minute series that
contains a fat-finger tick it is not a hypothetical.

**What MAD is.** MAD is the *median absolute deviation*: take the median of the
sample, measure how far each point sits from it, and take the median of those
distances.

    med = median(x)
    MAD = median(|x - med|)

Its breakdown point is 50%: up to half the sample can be arbitrarily corrupted
before MAD moves at all, where the standard deviation breaks on a single point.

**Why the 1.4826.** MAD is on a different scale from sigma, so a raw ratio to
it would not mean anything familiar. For Gaussian data MAD converges to
0.6745 * sigma (0.6745 is the 75th-percentile z, the quartile of the normal),
so scaling by 1 / 0.6745 = 1.4826 makes ``_MAD_SCALE * MAD`` a consistent
estimator of sigma. That is what lets the ratio below be read as "how many
standard deviations out", using the same units an analyst already has
intuition for:

    robust_z(x) = |x - med| / (_MAD_SCALE * MAD)

**Thresholds** live in :class:`~market_data.config.Settings`, not here, so they
are tunable without a code change:

    setting                    default   meaning
    outlier_mad_threshold      8.0       robust_z above which a return is flagged
    volume_spike_threshold     20.0      multiple of the local median volume

Eight sigma is deliberately far past the textbook three. Intraday futures
returns are strongly leptokurtic -- a 3-sigma minute happens many times a day
in normal trading -- so a conventional cutoff would flag thousands of perfectly
good bars and tell the user nothing. The bar is set where a hit is more likely
to be a bad print than a market move. The same reasoning sets 20x on volume:
an open, a close and a data release routinely carry several times the median,
and only an order of magnitude above it is worth a line in the report.

Both rules also require a minimum sample per contract (30 bars for returns, 50
for volume). Below that the estimators are too noisy to divide by, and a thin
back-month contract would otherwise generate a wall of spurious hits.
"""

from __future__ import annotations

from collections.abc import Iterable

import polars as pl

from market_data.domain.models import IssueCategory, QualityIssue, Severity
from market_data.domain.quality.details import BAR_VALUE_COLUMNS, bar_details, detail_fields
from market_data.domain.quality.rules import RuleContext

#: Consistency constant converting MAD to a sigma-equivalent scale, 1 / 0.6745.
#: See the module docstring: without it the ratio is in MAD units and the
#: threshold could not be reasoned about as a number of standard deviations.
_MAD_SCALE = 1.4826


def price_return_outliers(ctx: RuleContext) -> Iterable[QualityIssue]:
    """Flag bars whose close-to-close return is a robust-z outlier.

    Per contract:

        r_t        = ln(close_t) - ln(close_{t-1})
        med        = median(r)
        MAD        = median(|r - med|)
        robust_z_t = |r_t - med| / (1.4826 * MAD)
        hit        <=> robust_z_t > outlier_mad_threshold   (default 8.0)

    Log returns rather than percentage changes because they are symmetric in
    direction -- a halving and a doubling are the same distance from zero --
    which keeps the distribution centred and the deviation measure meaningful.

    Known weakness: ``r_t`` is taken between adjacent *rows*, with no regard for
    what separates them, so an overnight or weekend gap is scored as if it were
    a one-bar move. The generous threshold absorbs most of that, but a session
    boundary is genuinely not the same event as a within-session jump and would
    be better excluded outright.
    """
    if ctx.bars.is_empty():
        return []
    thr = ctx.settings.outlier_mad_threshold
    limit = ctx.settings.issue_detail_limit
    out: list[QualityIssue] = []
    for contract, grp in ctx.bars.group_by("contract", maintain_order=True):
        # `group_by` hands back the key as a tuple once it is composite; this
        # one is single-column, so accept either shape.
        name = str(contract[0] if isinstance(contract, tuple) else contract)
        # `close > 0` guards the log; a non-positive close is already reported
        # by the invalid-value checks, and here it would only produce a NaN.
        s = grp.sort("ts").filter(pl.col("close") > 0)
        if s.height < 30:
            # Too few observations for median/MAD to describe anything.
            continue
        r = s.select(
            pl.col("ts"),
            (pl.col("close").log() - pl.col("close").log().shift(1)).alias("ret"),
        ).drop_nulls("ret")  # the first bar has no predecessor
        med = r["ret"].median()
        mad = (r["ret"] - med).abs().median()
        if mad is None or mad == 0:
            # MAD is zero when over half the returns are identical -- a locked
            # or untraded stretch. Dividing by it would make every non-flat bar
            # infinitely far out, so the contract is skipped instead.
            continue
        z = (r["ret"] - med).abs() / (_MAD_SCALE * mad)
        # Carried on the row, not just used as a mask, so the evidence table can
        # show *how far out* each bar was rather than only that it crossed.
        hits = r.with_columns(mad_score=z).filter(pl.col("mad_score") > thr)
        # `r` holds only ts and the return, so join the bar back on: a reader
        # judging an extreme move needs the prices that produced it.
        hits = hits.join(s.select("ts", *BAR_VALUE_COLUMNS), on="ts", how="left")
        if hits.height:
            out.append(
                QualityIssue(
                    category=IssueCategory.OUTLIER,
                    code="price_return_outlier",
                    severity=Severity.WARNING,
                    contract=name,
                    frequency=ctx.frequency,
                    message=f"{hits.height} bar(s) with an extreme close-to-close return for {name}",
                    count=hits.height,
                    start_ts=hits["ts"].min(),
                    end_ts=hits["ts"].max(),
                    context={
                        # Carried so a reader can judge the hit without
                        # rerunning the check: the cutoff that was in force, and
                        # how large the worst flagged move actually was.
                        "mad_threshold": thr,
                        "max_abs_return": round(float(hits["ret"].abs().max()), 5),
                    },
                    **detail_fields(
                        bar_details(
                            hits.with_columns(
                                pl.col("ret").round(6).alias("log_return"),
                                pl.col("mad_score").round(1),
                            ),
                            limit,
                            columns=("log_return", "mad_score", *BAR_VALUE_COLUMNS),
                        ),
                        limit,
                        total=hits.height,
                    ),
                )
            )
    return out


def volume_spikes(ctx: RuleContext) -> Iterable[QualityIssue]:
    """Flag bars whose volume dwarfs the volume around them.

    Per contract:

        local_med_t = rolling_median(volume, window=50, min_samples=20)
        hit         <=> local_med_t > 0 and volume_t > volume_spike_threshold * local_med_t

    Deliberately a *ratio to a local median*, not the robust z-score used for
    returns. Volume is non-negative and heavily right-skewed rather than
    roughly symmetric, and on thin minute data more than half the bars can be
    identical (often zero), which drives MAD to zero and makes a z-score
    undefined exactly where it is needed. A multiple of the median has none of
    those failure modes and states the finding in the terms a trader would use:
    "twenty times normal".

    The window is *rolling* because volume has a strong intraday profile -- the
    open and the close are many times the midday rate -- so a single median for
    the whole series would flag every open and miss every quiet-hour anomaly.
    Fifty bars is long enough to be stable and short enough to track that
    profile; ``min_samples=20`` lets the check start early in each series
    instead of blanking the first 49 bars.

    Severity is INFO: a volume spike is usually a real market event (a data
    release, a roll, an expiry), so it is context for the reader rather than a
    defect.
    """
    if ctx.bars.is_empty():
        return []
    factor = ctx.settings.volume_spike_threshold
    limit = ctx.settings.issue_detail_limit
    out: list[QualityIssue] = []
    for contract, grp in ctx.bars.group_by("contract", maintain_order=True):
        name = str(contract[0] if isinstance(contract, tuple) else contract)
        # A null volume is a separate finding (`missing_volume` in values.py);
        # here it is treated as no activity so it cannot break the rolling median.
        s = grp.sort("ts").with_columns(pl.col("volume").fill_null(0))
        if s.height < 50 or s["volume"].sum() == 0:
            # Shorter than one window, or a contract with no recorded volume at
            # all -- nothing to compare against either way.
            continue
        # Trailing window, current bar included. A spike therefore nudges its
        # own baseline upward, but by one observation in fifty, which cannot
        # move a median enough to matter.
        med = s.select(pl.col("volume").rolling_median(window_size=50, min_samples=20)).to_series()
        # `med` is positionally aligned with `s` (same frame, already sorted).
        # The `med > 0` guard drops both the warm-up nulls and dead stretches,
        # where any non-zero bar would otherwise be an infinite multiple.
        # Kept on the row rather than only used as a mask: "80,000 lots" says
        # nothing without the baseline it is being compared against.
        s = s.with_columns(local_median=med)
        hits = s.filter((pl.col("local_median") > 0) & (pl.col("volume") > factor * pl.col("local_median")))
        if hits.height:
            out.append(
                QualityIssue(
                    category=IssueCategory.OUTLIER,
                    code="volume_spike",
                    severity=Severity.INFO,
                    contract=name,
                    frequency=ctx.frequency,
                    message=f"{hits.height} bar(s) with volume >{factor}x the local median for {name}",
                    count=hits.height,
                    start_ts=hits["ts"].min(),
                    end_ts=hits["ts"].max(),
                    **detail_fields(
                        bar_details(
                            hits.with_columns(
                                (pl.col("volume") / pl.col("local_median")).round(1).alias("x_median")
                            ),
                            limit,
                            # `volume` is already in BAR_VALUE_COLUMNS, so
                            # only the two derived figures are named here.
                            columns=("local_median", "x_median", *BAR_VALUE_COLUMNS),
                        ),
                        limit,
                        total=hits.height,
                    ),
                )
            )
    return out
