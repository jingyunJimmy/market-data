"""Normalise a raw vendor frame into the canonical bar schema.

This is where every messy real-world detail is dealt with once:

* vendor column names (``contract_symbol`` -> ``contract`` etc.)
* frequency detection (minute vs daily)
* the dataset's split timestamp semantics -- minute files carry a tz-naive
  *Chicago wall clock*, daily files carry a real UTC-midnight epoch
* DST edge cases (spring-forward gaps, fall-back ambiguity)
* type coercion, with unparseable values captured rather than crashing
* structural rejection of rows that cannot form a bar
* exact-duplicate collapsing within a batch (with a count)

Rows that are *suspicious but usable* (negative volume, ``high < low`` ...) are
kept here and surfaced later by the quality layer.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass, field

import polars as pl

from market_data.domain.models import (
    CANONICAL_SCHEMA,
    OHLC_COLUMNS,
    Frequency,
)
from market_data.ingestion.readers import ROW_NUM_COL, SOURCE_COL

# canonical name -> accepted source aliases (compared case-insensitively)
_COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "contract": ("contract", "contract_symbol", "symbol", "ticker", "instrument"),
    "root": ("root", "product_root", "product", "root_symbol"),
    "exchange": ("exchange", "venue", "mic", "operating_mic"),
    "open": ("open", "o", "open_price", "px_open"),
    "high": ("high", "h", "high_price", "px_high"),
    "low": ("low", "l", "low_price", "px_low"),
    "close": ("close", "c", "close_price", "px_close", "settle", "settlement"),
    "volume": ("volume", "v", "vol", "qty", "quantity"),
    "open_interest": ("open_interest", "oi", "openinterest"),
}

# columns we read for timestamp construction but never store verbatim
_TS_SOURCE_COLUMNS = (
    "timestamp_chicago_wall",
    "timestamp_ms",
    "timestamp",
    "ts",
    "datetime",
    "time",
    "date",
    "trading_date",
)

_REQUIRED_FOR_BAR = ("contract", *OHLC_COLUMNS)

REJECT_MISSING_COLUMNS = "missing_required_columns"
REJECT_NO_CONTRACT = "missing_contract"
REJECT_BAD_TIMESTAMP = "unparseable_timestamp"
REJECT_DST_LOCAL_TIME = "ambiguous_or_nonexistent_local_time"
REJECT_BAD_PRICE = "unparseable_price"
REJECT_NO_OHLC = "missing_ohlc"

#: A valid row that lost the ``(contract, frequency, ts)`` tie-break to a later
#: row in the same file. Not a rejection: the row is well-formed, it was
#: displaced by the last-write-wins rule.
SUPERSEDED_IN_FILE = "superseded_in_file"


@dataclass
class NormalizeResult:
    clean: pl.DataFrame
    rejected: pl.DataFrame  # __source_file, __row_number, __reason (+ original cols)
    frequency: Frequency
    rows_read: int
    rows_deduplicated: int = 0
    rejections_by_reason: dict[str, int] = field(default_factory=dict)
    #: Canonical rows displaced by a later row for the same instant, each
    #: carrying the ``winning_row_hash`` of the row that replaced it.
    superseded: pl.DataFrame = field(default_factory=lambda: _empty_superseded())
    rows_superseded: int = 0


def _resolve_aliases(columns: list[str]) -> dict[str, str]:
    """Map source column name -> canonical name (only for recognised aliases)."""
    lookup = {c.lower(): c for c in columns}
    mapping: dict[str, str] = {}
    for canonical, aliases in _COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in lookup:
                mapping[lookup[alias]] = canonical
                break
    return mapping


def detect_frequency(
    df: pl.DataFrame, *, path_hint: str = "", override: Frequency | None = None
) -> Frequency:
    if override is not None:
        return override
    hint = path_hint.lower()
    if "/minute/" in hint or "minute" in hint.split("/")[-1:][0:1]:
        return Frequency.MINUTE
    if "/daily/" in hint:
        return Frequency.DAILY
    cols = {c.lower() for c in df.columns}
    if {"minute_of_day", "timestamp_chicago_wall"} & cols:
        return Frequency.MINUTE
    if "open_interest" in cols:
        return Frequency.DAILY
    return Frequency.MINUTE


def _naive_datetime(name: str) -> pl.Expr:
    """Parse a column that may already be Datetime *or* an ISO string -> naive us Datetime."""
    col = pl.col(name)
    return pl.coalesce(
        col.cast(pl.Datetime("us"), strict=False),
        col.cast(pl.String).str.to_datetime(strict=False, time_zone=None, time_unit="us"),
    )


def _to_date(name: str) -> pl.Expr:
    col = pl.col(name)
    return pl.coalesce(
        col.cast(pl.Date, strict=False),
        col.cast(pl.String).str.to_date(strict=False),
        col.cast(pl.String).str.to_datetime(strict=False).dt.date(),
    )


def _build_timestamp(df: pl.DataFrame, frequency: Frequency, source_tz: str) -> pl.DataFrame:
    """Add a ``ts`` column (tz-aware UTC instant). Unparseable / DST-invalid -> null.

    The dataset splits timestamp semantics by frequency, and its docs are
    explicit that ``timestamp_ms`` must NOT be read as a UTC instant for minute
    data, nor auto-converted UTC<->Chicago. So:

    * daily -- ``timestamp_ms`` (or the ``date`` column) is already a real
      00:00:00 UTC label. Parse it and *stamp* it UTC (``replace_time_zone``):
      no clock shift, because it is UTC already. Negative epochs (pre-1970) are
      fine.
    * minute -- the source is a tz-naive Chicago *wall clock* (its integer
      ``timestamp_ms`` is just a label, deliberately ignored here). We take the
      wall-clock components, *localise* them to America/Chicago
      (``replace_time_zone`` -- attaches the zone, digits unchanged, so the
      correct DST offset for that date is applied), then *convert* to UTC
      (``convert_time_zone`` -- digits shift, instant preserved) to get a true
      instant for storage/analytics. Trading-date derivation later converts
      ``ts`` back to the exchange tz.

    Why localise-then-convert rather than subtract a fixed offset: the same
    wall time maps to a different UTC instant by season (CST = UTC-6 vs
    CDT = UTC-5); only the tz database knows which applies on a given date.
    """
    cols = {c.lower(): c for c in df.columns}

    if frequency is Frequency.DAILY:
        if "date" in cols:
            ts = _to_date(cols["date"]).cast(pl.Datetime("us"))
        elif "timestamp_ms" in cols:
            ts = pl.from_epoch(pl.col(cols["timestamp_ms"]).cast(pl.Int64, strict=False), "ms").cast(
                pl.Datetime("us")
            )
        elif "timestamp" in cols:
            ts = _naive_datetime(cols["timestamp"])
        else:
            ts = pl.lit(None, dtype=pl.Datetime("us"))
        # Stamp, don't convert: the daily label is UTC midnight already.
        return df.with_columns(ts.dt.replace_time_zone("UTC").alias("ts"))

    # minute: prefer the explicit wall-clock column; fall back to other naive
    # datetime columns if a vendor file lacks it.
    if "timestamp_chicago_wall" in cols:
        wall = _naive_datetime(cols["timestamp_chicago_wall"])
    elif "timestamp" in cols:
        wall = _naive_datetime(cols["timestamp"])
    elif "datetime" in cols:
        wall = _naive_datetime(cols["datetime"])
    else:
        wall = pl.lit(None, dtype=pl.Datetime("us"))

    # 1. localise: "this wall clock IS America/Chicago local time" (no digit shift)
    # 2. convert:  express that same instant in UTC (digits shift by the DST offset)
    # DST edge cases -> null, rejected downstream with an explicit reason:
    #   spring-forward gap  -> the wall time never existed  (non_existent="null")
    #   fall-back overlap   -> the wall time is ambiguous    (ambiguous="null")
    # __wall keeps the parsed naive value so Step 8 can tell a DST-invalid time
    # (ts null, __wall set) apart from a timestamp that never parsed (both null).
    localised = wall.dt.replace_time_zone(
        source_tz, ambiguous="null", non_existent="null"
    ).dt.convert_time_zone("UTC")
    return df.with_columns(localised.alias("ts"), wall.alias("__wall"))


def _row_hash_expr() -> pl.Expr:
    """Stable identity hash for a bar -- the dedup key and the DuckDB primary key.

    It is derived only from the *business* fields that define a bar. Provenance
    columns (``source_file``, ``ingested_at``, ``__row_number`` ...) are excluded
    on purpose, so the same bar re-ingested from any file yields the same hash
    and is dropped by ``ON CONFLICT (row_hash) DO NOTHING``.

    SHA-1 (not Polars' ``.hash()``) because the value must be stable across
    processes and library versions to work as a persisted key.
    """
    parts = [
        pl.col("contract").fill_null(""),
        pl.col("frequency"),  # keeps a minute bar and a daily bar from colliding
        # Strip the tz before stringifying so an instant has one canonical form.
        pl.col("ts").dt.replace_time_zone(None).cast(pl.String).fill_null(""),
        # round(10) collapses float representation noise past the 10th decimal.
        *(pl.col(c).round(10).cast(pl.String).fill_null("") for c in OHLC_COLUMNS),
        pl.col("volume").cast(pl.String).fill_null(""),
    ]
    # fill_null("") on every part: concat_str returns null if any input is null,
    # which would make the whole hash null for a bar with e.g. no volume.
    joined = pl.concat_str(parts, separator="|")
    return joined.map_elements(lambda s: hashlib.sha1(s.encode()).hexdigest(), return_dtype=pl.String)


def normalize(
    raw: pl.DataFrame,
    *,
    frequency: Frequency | None = None,
    path_hint: str = "",
    source_tz: str = "America/Chicago",
    daily_tz: str = "America/Chicago",
    ingested_at: dt.datetime | None = None,
) -> NormalizeResult:
    # --- Step 0: setup -------------------------------------------------------
    # Record the input size, stamp the batch, and settle the frequency (explicit
    # override > path hint like /minute/ or /daily/ > column signature > minute).
    rows_read = raw.height
    ingested_at = ingested_at or dt.datetime.now(dt.UTC)
    freq = detect_frequency(raw, path_hint=path_hint, override=frequency)

    # accept frames that skipped the reader (direct DataFrame input, tests, API)
    if SOURCE_COL not in raw.columns:
        raw = raw.with_columns(pl.lit("<memory>").alias(SOURCE_COL))
    if ROW_NUM_COL not in raw.columns:
        raw = raw.with_columns(pl.int_range(1, pl.len() + 1, dtype=pl.Int64).alias(ROW_NUM_COL))

    # --- Step 1: whole-file sanity check -----------------------------------
    # No OHLC column at all *and* no timestamp column -> this file is not bar
    # data. Reject every row with one reason rather than limping on.
    missing_structural = [c for c in ("open", "high", "low", "close") if c not in _present(raw)]
    if missing_structural and not _has_any_ts(raw):
        # Nothing usable in this file at all. Shaped like every other reject
        # report -- source_file, row_number, reason -- because callers persist
        # it through `record_rejections`, which selects exactly those columns.
        rejected = raw.select(
            pl.col(SOURCE_COL).alias("source_file"),
            pl.col(ROW_NUM_COL).alias("row_number"),
            pl.lit(REJECT_MISSING_COLUMNS).alias("reason"),
        )
        return NormalizeResult(
            clean=_empty_canonical(),
            rejected=rejected,
            frequency=freq,
            rows_read=rows_read,
            rejections_by_reason={REJECT_MISSING_COLUMNS: rows_read},
        )

    # --- Step 2: vendor column names -> canonical names --------------------
    # e.g. contract_symbol/symbol/ticker -> contract, px_open/o -> open,
    # settle/settlement -> close. Matched case-insensitively via _COLUMN_ALIASES.
    df = raw.rename(_resolve_aliases(raw.columns))

    # --- Step 3: materialise any missing canonical columns as all-null -----
    # so later expressions never blow up on an absent column.
    for col in (*OHLC_COLUMNS,):
        if col not in df.columns:
            df = df.with_columns(pl.lit(None, dtype=pl.String).alias(col))
    for col in ("volume", "open_interest"):
        if col not in df.columns:
            df = df.with_columns(pl.lit(None).alias(col))
    if "contract" not in df.columns:
        df = df.with_columns(pl.lit(None, dtype=pl.String).alias("contract"))

    df = df.with_columns(pl.lit(freq.value).alias("frequency"))

    # --- Step 4: build the `ts` column (tz-aware UTC) ---------------------
    # The dataset splits timestamp semantics by frequency: daily files carry a
    # real UTC-midnight epoch (stamped UTC as-is); minute files carry a tz-naive
    # Chicago wall clock that must be localised (DST gaps/ambiguity -> null) and
    # converted to UTC. See _build_timestamp.
    df = _build_timestamp(df, freq, source_tz)

    # --- Step 5: typed coercion, tracking parse failures ---------------------
    # Computed in a single with_columns so every reference resolves against the
    # still-string input (evaluating after the columns are replaced would hide
    # the failures). "non-null string that fails to parse" is a real reject
    # reason (unparseable_price); an already-null/empty cell is just missing data.
    orig = {c: pl.col(c).cast(pl.String).str.strip_chars() for c in OHLC_COLUMNS}
    parsed = {c: orig[c].cast(pl.Float64, strict=False) for c in OHLC_COLUMNS}
    price_parse_failed = pl.any_horizontal(
        *(orig[c].is_not_null() & parsed[c].is_null() for c in OHLC_COLUMNS)
    )
    df = df.with_columns(
        *(parsed[c].alias(c) for c in OHLC_COLUMNS),
        pl.col("volume").cast(pl.String).str.strip_chars().cast(pl.Int64, strict=False),
        pl.col("open_interest").cast(pl.String).str.strip_chars().cast(pl.Int64, strict=False),
        price_parse_failed.alias("__price_parse_failed"),
    )

    # --- Step 6: contract / root / exchange -------------------------------
    df = df.with_columns(
        pl.col("contract").cast(pl.String).str.strip_chars().replace("", None),
    )
    if "root" not in df.columns:
        # Derive the product root by stripping the futures month code (one of
        # FGHJKMNQUVXZ) + year digits, e.g. GCM26 -> GC, CLZ25 -> CL.
        df = df.with_columns(
            pl.col("contract").str.extract(r"^([A-Za-z0-9]+?)[FGHJKMNQUVXZ]\d{1,2}$", 1).alias("root")
        )
    df = df.with_columns(
        pl.col("root").cast(pl.String).fill_null(pl.col("contract").str.slice(0, 2)),
        pl.col("exchange").cast(pl.String).fill_null("")
        if "exchange" in df.columns
        else pl.lit("").alias("exchange"),
    )

    # --- Step 7: trading_date -- the exchange session date the bar belongs to
    # For minute bars this is NOT the UTC calendar date: convert ts back to the
    # exchange tz first, because a session spans midnight UTC.
    raw_lower = {c.lower(): c for c in raw.columns}
    if "trading_date" in raw_lower:
        td = _to_date(raw_lower["trading_date"])
    elif freq is Frequency.DAILY and "date" in raw_lower:
        td = _to_date(raw_lower["date"])
    elif freq is Frequency.DAILY:
        td = pl.col("ts").dt.date()
    else:
        td = pl.col("ts").dt.convert_time_zone(daily_tz).dt.date()
    df = df.with_columns(td.alias("trading_date"))

    # --- Step 8: classify rejections --------------------------------------
    # First matching branch wins. Only *structural* failures are rejected here;
    # "suspicious but usable" rows (negative volume, high < low, ...) stay in
    # `clean` and are surfaced later by the quality layer.
    wall_present = "__wall" in df.columns
    reason = (
        pl.when(pl.col("contract").is_null())
        .then(pl.lit(REJECT_NO_CONTRACT))
        .when(pl.col("__price_parse_failed"))
        .then(pl.lit(REJECT_BAD_PRICE))
        .when(pl.all_horizontal(pl.col(c).is_null() for c in OHLC_COLUMNS))
        .then(pl.lit(REJECT_NO_OHLC))
        .when(pl.any_horizontal(pl.col(c).is_null() for c in OHLC_COLUMNS))
        .then(pl.lit(REJECT_NO_OHLC))
        # ts is null but we did parse a wall clock -> it fell in a DST gap or
        # ambiguous hour (distinct reason from a timestamp that never parsed).
        .when(pl.col("ts").is_null() & pl.col("__wall").is_not_null() if wall_present else pl.lit(False))
        .then(pl.lit(REJECT_DST_LOCAL_TIME))
        .when(pl.col("ts").is_null())
        .then(pl.lit(REJECT_BAD_TIMESTAMP))
        .otherwise(None)
        .alias("__reason")
    )
    df = df.with_columns(reason)

    rejected = df.filter(pl.col("__reason").is_not_null())
    clean = df.filter(pl.col("__reason").is_null())

    # --- Step 9: finalise clean rows -------------------------------------------
    # Lock in ts dtype, attach provenance, compute the identity hash, then the
    # in-file dedup pass, then project/sort to exactly the canonical schema
    # (drops all __-prefixed working columns).
    clean = clean.with_columns(
        pl.col("ts").cast(pl.Datetime("us", "UTC")),
        pl.lit(str(_first_source(raw))).alias("source_file"),
        pl.lit(ingested_at).cast(pl.Datetime("us", "UTC")).alias("ingested_at"),
    )
    clean = clean.with_columns(_row_hash_expr().alias("row_hash"))

    # Dedup pass 1 -- byte-identical rows within this one file. Nothing is lost,
    # so these are only counted, never reported per-row.
    before = clean.height
    clean = clean.unique(subset=["row_hash"], keep="first", maintain_order=True)
    deduped = before - clean.height

    # Dedup pass 2 -- rows that agree on the instant but disagree on the values.
    # One bar per instant is the invariant the whole read side depends on, so the
    # conflict is resolved here rather than left for analytics to trip over.
    # Last write wins, ordered by position in the file, and the loser is kept in
    # full so the choice can be audited.
    clean, superseded = _resolve_instant_conflicts(clean, ingested_at)

    clean = clean.select([pl.col(name).cast(dtype) for name, dtype in CANONICAL_SCHEMA.items()]).sort(
        "contract", "ts"
    )

    # --- Step 10: shape the reject report ---------------------------------
    # Keep only what's needed to trace a bad row back to its origin, plus a
    # per-reason tally for the ingestion summary.
    rej_out = rejected.select(
        [
            pl.col(SOURCE_COL).alias("source_file"),
            pl.col(ROW_NUM_COL).alias("row_number"),
            pl.col("__reason").alias("reason"),
        ]
    )
    counts = dict(rej_out.group_by("reason").len().iter_rows()) if rej_out.height else {}

    return NormalizeResult(
        clean=clean,
        rejected=rej_out,
        frequency=freq,
        rows_read=rows_read,
        rows_deduplicated=deduped,
        rejections_by_reason=counts,
        superseded=superseded,
        rows_superseded=superseded.height,
    )


def _resolve_instant_conflicts(
    clean: pl.DataFrame, ingested_at: dt.datetime
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Enforce one bar per ``(contract, frequency, ts)``; return (winners, losers).

    "Latest" means latest *in the file*, ordered by ``__row_number``, which the
    readers assign so the choice is deterministic and does not depend on how
    Polars happens to order a group.

    Losers keep every canonical column plus the ``row_hash`` of the row that
    displaced them, so a stored bar can be traced back to what it replaced.
    """
    key = ["contract", "frequency", "ts"]
    if clean.height == 0:
        return clean, _empty_superseded()

    ordered = clean.sort(ROW_NUM_COL)
    winners = ordered.unique(subset=key, keep="last", maintain_order=True)
    if winners.height == clean.height:  # the common case: no conflicts at all
        return clean, _empty_superseded()

    losers = ordered.join(winners.select("row_hash"), on="row_hash", how="anti").join(
        winners.select(*key, pl.col("row_hash").alias("winning_row_hash")),
        on=key,
        how="left",
    )
    superseded = losers.select(
        pl.lit(SUPERSEDED_IN_FILE).alias("reason"),
        pl.col("winning_row_hash"),
        *(pl.col(name).cast(dtype) for name, dtype in CANONICAL_SCHEMA.items()),
        pl.lit(ingested_at).cast(pl.Datetime("us", "UTC")).alias("superseded_at"),
    )
    return winners, superseded


def _empty_superseded() -> pl.DataFrame:
    return pl.DataFrame(
        schema={
            "reason": pl.String,
            "winning_row_hash": pl.String,
            **CANONICAL_SCHEMA,
            "superseded_at": pl.Datetime("us", "UTC"),
        }
    )


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #


def _present(df: pl.DataFrame) -> set[str]:
    mapping = _resolve_aliases(df.columns)
    return set(mapping.values())


def _has_any_ts(df: pl.DataFrame) -> bool:
    lower = {c.lower() for c in df.columns}
    return any(c in lower for c in _TS_SOURCE_COLUMNS)


def _first_source(df: pl.DataFrame) -> str:
    if SOURCE_COL in df.columns and df.height:
        return str(df[SOURCE_COL][0])
    return "<memory>"


def _empty_canonical() -> pl.DataFrame:
    return pl.DataFrame(schema=CANONICAL_SCHEMA)
