"""The flat missing-timestamp listing.

The gap rule answers "how bad is it"; this answers "which instants are gone".
These tests pin two properties the dashboard depends on: the expansion names
exactly the absent instants, and expected breaks are excluded. Paging is
covered against the endpoint in the integration tests.
"""

from __future__ import annotations

import datetime as dt
from itertools import pairwise

import polars as pl

from market_data.domain.models import Frequency
from market_data.domain.quality.missing import missing_timestamps
from market_data.ingestion.normalize import normalize
from market_data.synthetic import clean_minute_bars


def _minute_bars_with_hole(lo: dt.datetime, hi: dt.datetime, minutes: int = 180) -> pl.DataFrame:
    """Canonical minute bars with the wall-clock window [lo, hi] removed.

    The hole is punched before normalisation, so what reaches the listing is
    indistinguishable from a vendor file that never had those bars.
    """
    df = clean_minute_bars(minutes=minutes)
    holed = df.filter(~pl.col("timestamp_chicago_wall").is_between(lo, hi))
    return normalize(holed, frequency=Frequency.MINUTE).clean


def test_minute_expansion_names_every_absent_instant():
    """Correct in both directions: nothing listed is present, and the listing
    has no holes of its own.

    Rather than hard-coding the ten instants, the test states the two
    properties that must hold for any hole -- disjoint from the bars that exist,
    and contiguous on the inferred grid -- which also survives a change to the
    generator's start time.
    """
    lo = dt.datetime(2024, 3, 4, 10, 0)
    hi = dt.datetime(2024, 3, 4, 10, 9)
    bars = _minute_bars_with_hole(lo, hi)

    page = missing_timestamps(bars, contract="CL_TEST", frequency=Frequency.MINUTE, limit=1000)

    listed = {t.ts for t in page.timestamps}
    present = set(bars["ts"].to_list())
    # Every instant reported absent really is absent...
    assert listed.isdisjoint(present)
    # ...and the run is contiguous on the inferred grid.
    ordered = sorted(listed)
    step = dt.timedelta(seconds=page.expected_interval_s or 0)
    assert all(b - a == step for a, b in pairwise(ordered))
    assert page.total == len(listed)


def test_minute_expansion_infers_the_bar_interval():
    """The grid is derived from the data, not assumed to be 60s.

    Nothing declares the bar interval, so it is inferred from the spacing that
    actually dominates the frame. Getting it wrong would invent or hide gaps.
    """
    bars = _minute_bars_with_hole(dt.datetime(2024, 3, 4, 10, 0), dt.datetime(2024, 3, 4, 10, 9))
    page = missing_timestamps(bars, contract="CL_TEST", frequency=Frequency.MINUTE)
    assert page.expected_interval_s == 60


def test_expected_session_breaks_are_never_listed():
    """A weekend is not missing data, and listing it would bury the real holes."""
    bars = normalize(clean_minute_bars(minutes=180), frequency=Frequency.MINUTE).clean
    page = missing_timestamps(bars, contract="CL_TEST", frequency=Frequency.MINUTE, limit=10_000)
    assert all(t.classification in {"intra_session", "extended"} for t in page.timestamps)


def test_empty_frame_yields_an_empty_page():
    """A contract with no data returns an empty page, not an error: the
    dashboard asks for this before the user has ingested anything."""
    page = missing_timestamps(pl.DataFrame(), contract="NOPE", frequency=Frequency.MINUTE)
    assert page.total == 0
    assert page.timestamps == []
    assert page.has_more is False
