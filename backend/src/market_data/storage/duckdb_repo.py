"""DuckDB implementation of :class:`BarRepository`.

DuckDB is a good fit here: embedded (no server), columnar, reads/writes Parquet
natively, and it filters millions of bars by contract + date range fast enough
to sit directly behind an interactive dashboard.
"""

from __future__ import annotations

import datetime as dt
import threading
from pathlib import Path

import duckdb
import polars as pl

from market_data.domain.models import (
    CANONICAL_COLUMNS,
    ContractSummary,
    Frequency,
    IngestionResult,
)

_SCHEMA_SQL = (Path(__file__).parent / "schema.sql").read_text()

#: A bar's identity. One row per key is the invariant the read side relies on.
_BAR_KEY = ("contract", "frequency", "ts")

#: Everything the upsert overwrites when a later run wins the key.
_BAR_UPDATABLE = tuple(c for c in CANONICAL_COLUMNS if c not in _BAR_KEY)

#: Reason recorded when a *previously stored* bar is displaced by a later run.
SUPERSEDED_BY_RUN = "superseded_by_run"

# ``frame`` in the statements below is NOT a database table. It is the Python
# local of that name in the calling method, picked up by DuckDB's *replacement
# scan*: an unresolved table name is looked up in the caller's frame, and a
# DataFrame found there is read in place, zero-copy. That is why those locals
# carry a ``noqa: F841`` -- a linter sees an unused assignment; the SQL string
# is the reference.

# Upsert the batch, last write wins.
#
#   INSERT INTO bars SELECT * FROM frame
#   ON CONFLICT (contract, frequency, ts) DO UPDATE SET open = excluded.open, ...
#
# ``SELECT *`` is safe because the caller passes CANONICAL_COLUMNS in schema
# order. On a key we already hold, every non-key column is overwritten from
# ``excluded`` (DuckDB's alias for the row that lost the conflict, i.e. the
# incoming one), so a correction lands in place instead of erroring.
# The key columns are excluded from the SET list: they are what matched.
_UPSERT_BARS_SQL = (
    f"INSERT INTO bars SELECT * FROM frame "
    f"ON CONFLICT ({', '.join(_BAR_KEY)}) DO UPDATE SET "
    + ", ".join(f"{c} = excluded.{c}" for c in _BAR_UPDATABLE)
)

# Snapshot the rows this batch is about to overwrite, BEFORE the upsert runs.
# Order matters absolutely: the upsert rewrites those rows in place, so once it
# has run the old values are gone and there is nothing left to snapshot.
#
# The SELECT list assembles one whole superseded_bars row positionally:
#
#   ?                      -> run_id            (bound to the caller's run_id)
#   'superseded_by_run'    -> reason            (constant tag for this path)
#   f.row_hash             -> winning_row_hash  (the INCOMING row, the winner)
#   b.*                    -> the bar columns   (the STORED row, the loser)
#   now()                  -> superseded_at
#
# Note b and f swap roles: the values come from the old row, the pointer from
# the new one. The table always holds the loser plus a link to its replacement.
#
# ``b.*`` relies on superseded_bars repeating the bars columns in the same
# order (see schema.sql). Adding a column to one table only is caught as an
# arity mismatch; adding to both at different positions would misalign silently.
#
# JOIN ... USING (contract, frequency, ts) joins on the primary key itself, so
# it asks: which instants in this batch do we already hold? The join is inner,
# so brand-new instants match nothing and produce no audit row.
#
# WHERE b.row_hash <> f.row_hash is the "is this actually a change?" test.
# Re-ingesting an identical file makes every row overwrite itself: the key
# conflicts, but the content is unchanged. Without this filter each repeat run
# would fabricate a full set of audit rows and the quality report would raise
# conflicts that never happened. It works because row_hash covers only the
# business fields, so the same bar hashes identically from any file or run.
_CAPTURE_DISPLACED_SQL = f"""
INSERT INTO superseded_bars
SELECT ?, '{SUPERSEDED_BY_RUN}', f.row_hash, b.*, now()
FROM bars b
JOIN frame f USING ({", ".join(_BAR_KEY)})
WHERE b.row_hash <> f.row_hash
"""

# The contract catalogue: one row per (contract, frequency), which is what the
# GROUP BY collapses to. root and exchange are constant within a group, so
# any_value picks one without the cost of a real aggregate. The date bounds let
# a client open on a range that actually contains data instead of guessing.
_CATALOGUE_SQL = """
SELECT contract,
       any_value(root)     AS root,
       any_value(exchange) AS exchange,
       frequency,
       min(trading_date)   AS first_date,
       max(trading_date)   AS last_date,
       count(*)            AS bars
FROM bars
GROUP BY contract, frequency
ORDER BY contract, frequency
"""


class DuckDbRepository:
    def __init__(self, db_path: Path | str = ":memory:") -> None:
        self._db_path = str(db_path)
        if self._db_path != ":memory:":
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = duckdb.connect(self._db_path)
        self._local = threading.local()
        self._configure(self._conn)

    @staticmethod
    def _configure(conn: duckdb.DuckDBPyConnection) -> None:
        # Pin the session zone: TIMESTAMPTZ otherwise materialises in whatever
        # zone the host happens to be in, making responses machine-dependent.
        conn.execute("SET TimeZone = 'UTC'")

    @property
    def _cx(self) -> duckdb.DuckDBPyConnection:
        """A cursor onto the same database, private to the calling thread.

        FastAPI runs synchronous endpoints on a worker pool, and a single DuckDB
        connection object cannot serve concurrent queries -- interleaved
        execute/fetch calls silently return each other's (or empty) results.
        ``cursor()`` gives every thread its own handle onto the one database.
        """
        cursor = getattr(self._local, "cursor", None)
        if cursor is None:
            cursor = self._conn.cursor()
            self._configure(cursor)
            self._local.cursor = cursor
        return cursor

    # -- lifecycle ---------------------------------------------------------- #

    def initialise(self) -> None:
        self._cx.execute(_SCHEMA_SQL)
        self._assert_current_schema()

    def _assert_current_schema(self) -> None:
        """Fail loudly on a database created before the composite bar key.

        ``CREATE TABLE IF NOT EXISTS`` leaves an existing table alone, so a
        store built under the old ``row_hash`` primary key would silently keep
        it and the upsert would then fail with an opaque constraint error. The
        store is a rebuildable artefact, so the fix is to delete and re-ingest.
        """
        rows = self._cx.execute(
            "SELECT constraint_text FROM duckdb_constraints() "
            "WHERE table_name = 'bars' AND constraint_type = 'PRIMARY KEY'"
        ).fetchall()
        expected = f"PRIMARY KEY({', '.join(_BAR_KEY)})"
        if rows and rows[0][0] != expected:
            raise RuntimeError(
                f"{self._db_path} predates the one-bar-per-instant key "
                f"(found {rows[0][0]!r}, expected {expected!r}). "
                "Delete the database file and re-ingest; it is rebuildable."
            )

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> DuckDbRepository:
        self.initialise()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- writes ----------------------------------------------------------- #

    def insert_bars(self, bars: pl.DataFrame, *, run_id: str | None = None) -> int:
        """Upsert canonical bars; return the number of *new* instants stored.

        Last write wins on ``(contract, frequency, ts)``. A row that carried
        different values for an instant already held is snapshotted into
        ``superseded_bars`` first, so the overwrite stays auditable.

        The return value counts only instants that did not exist before, so
        re-ingesting a file reports ``0`` whether the values matched or were
        corrections. Corrections show up as ``superseded_bars`` rows instead.

        The caller must guarantee one row per key in ``bars``; DuckDB rejects a
        batch that conflicts with itself. ``normalize`` enforces that.
        """
        if bars.is_empty():
            return 0
        # Projected to schema order because both statements below use SELECT *.
        frame = bars.select(CANONICAL_COLUMNS)  # noqa: F841 - referenced in SQL below
        # 1. Snapshot what is about to be overwritten. Must precede the upsert.
        self._cx.execute(_CAPTURE_DISPLACED_SQL, [run_id])
        # 2. Upsert, and derive the return value from the row count on either
        #    side. An upsert's own rowcount counts every row it touched, which
        #    mixes inserts with updates; the diff isolates the instants that
        #    did not exist before, so a pure re-ingest reports 0.
        before = self._count()
        self._cx.execute(_UPSERT_BARS_SQL)
        return self._count() - before

    def record_run(self, result: IngestionResult) -> None:
        self._cx.execute(
            """
            INSERT INTO ingestion_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (run_id) DO NOTHING
            """,
            [
                result.run_id,
                result.source,
                result.started_at,
                result.finished_at,
                result.rows_read,
                result.rows_ingested,
                result.rows_rejected,
                result.rows_deduplicated,
                result.rows_superseded,
                result.contracts,
            ],
        )

    def record_rejections(self, run_id: str, rejected: pl.DataFrame) -> None:
        if rejected.is_empty():
            return
        # The trailing select() is what makes SELECT * safe: it pins the column
        # order to the rejected_rows DDL, since with_columns appends run_id last.
        frame = (  # noqa: F841 - resolved by DuckDB's replacement scan in the SQL below
            rejected.select("source_file", "row_number", "reason")
            .with_columns(pl.lit(run_id).alias("run_id"))
            .select("run_id", "source_file", "row_number", "reason")
        )
        self._cx.execute("INSERT INTO rejected_rows SELECT * FROM frame")

    def record_superseded(self, run_id: str, superseded: pl.DataFrame) -> None:
        """Append the valid-but-displaced rows a run resolved within one file."""
        if superseded.is_empty():
            return
        # Same shape the superseded_by_run path builds in SQL, assembled in
        # Polars instead: run_id, reason, winning pointer, the bar, timestamp.
        # The explicit select() pins that order to the DDL for the SELECT *.
        frame = (  # noqa: F841 - resolved by DuckDB's replacement scan in the SQL below
            superseded.with_columns(pl.lit(run_id).alias("run_id")).select(
                "run_id",
                "reason",
                "winning_row_hash",
                *CANONICAL_COLUMNS,
                "superseded_at",
            )
        )
        self._cx.execute("INSERT INTO superseded_bars SELECT * FROM frame")

    # -- reads ------------------------------------------------------------ #

    def contracts(self) -> list[ContractSummary]:
        rows = self._cx.execute(_CATALOGUE_SQL).pl().to_dicts()
        return [ContractSummary(**row) for row in rows]

    def load_bars(
        self,
        *,
        contract: str | None = None,
        frequency: Frequency = Frequency.MINUTE,
        start: dt.date | None = None,
        end: dt.date | None = None,
    ) -> pl.DataFrame:
        clauses = ["frequency = ?"]
        params: list[object] = [frequency.value]
        if contract:
            clauses.append("contract = ?")
            params.append(contract)
        if start:
            clauses.append("trading_date >= ?")
            params.append(start)
        if end:
            clauses.append("trading_date <= ?")
            params.append(end)
        sql = (
            f"SELECT {', '.join(CANONICAL_COLUMNS)} FROM bars "
            f"WHERE {' AND '.join(clauses)} ORDER BY contract, ts"
        )
        return self._cx.execute(sql, params).pl()

    def load_superseded(
        self,
        *,
        contract: str | None = None,
        frequency: Frequency = Frequency.MINUTE,
        start: dt.date | None = None,
        end: dt.date | None = None,
    ) -> pl.DataFrame:
        """Bars displaced by the last-write-wins rule, for the same filter.

        Ingestion resolves instant conflicts, so they can no longer be found in
        ``bars``. The quality layer reads them from here instead, which is why
        resolving them does not make them invisible.
        """
        clauses = ["frequency = ?"]
        params: list[object] = [frequency.value]
        if contract:
            clauses.append("contract = ?")
            params.append(contract)
        if start:
            clauses.append("trading_date >= ?")
            params.append(start)
        if end:
            clauses.append("trading_date <= ?")
            params.append(end)
        sql = (
            f"SELECT reason, winning_row_hash, {', '.join(CANONICAL_COLUMNS)} "
            f"FROM superseded_bars WHERE {' AND '.join(clauses)} ORDER BY contract, ts"
        )
        return self._cx.execute(sql, params).pl()

    # -- internals ------------------------------------------------------ #

    def _count(self) -> int:
        row = self._cx.execute("SELECT count(*) FROM bars").fetchone()
        return int(row[0]) if row else 0
