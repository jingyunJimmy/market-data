-- Canonical bars: exactly one row per (contract, frequency, ts).
--
-- The primary key is a bar's *identity* (which instrument, which frequency,
-- which instant), not its contents. So when a later ingest carries a different
-- OHLCV for an instant we already hold, the upsert in `insert_bars` overwrites
-- it: last write wins. `frequency` must be in the key because a minute bar and
-- a daily bar for the same contract can share a midnight timestamp.
--
-- Nothing is lost by that resolution. Every displaced row is written to
-- `superseded_bars` in full, tagged with the row_hash of the row that replaced
-- it, so the decision is auditable and reversible.
--
-- `row_hash` is a content hash over the business fields (contract, frequency,
-- ts, OHLC, volume). It no longer enforces uniqueness -- the composite key does
-- that -- but it is how a superseded row is tied back to its replacement, and
-- how byte-identical rows are collapsed inside a single file.
CREATE TABLE IF NOT EXISTS bars (
    contract       VARCHAR NOT NULL,
    root           VARCHAR,
    exchange       VARCHAR,
    frequency      VARCHAR NOT NULL,
    ts             TIMESTAMP WITH TIME ZONE NOT NULL,
    trading_date   DATE,
    open           DOUBLE,
    high           DOUBLE,
    low            DOUBLE,
    close          DOUBLE,
    volume         BIGINT,
    open_interest  BIGINT,
    source_file    VARCHAR,
    row_hash       VARCHAR NOT NULL,
    ingested_at    TIMESTAMP WITH TIME ZONE,
    PRIMARY KEY (contract, frequency, ts)
);

CREATE TABLE IF NOT EXISTS ingestion_runs (
    run_id             VARCHAR PRIMARY KEY,
    source             VARCHAR,
    started_at         TIMESTAMP WITH TIME ZONE,
    finished_at        TIMESTAMP WITH TIME ZONE,
    rows_read          BIGINT,
    rows_ingested      BIGINT,
    rows_rejected      BIGINT,
    rows_deduplicated  BIGINT,
    rows_superseded    BIGINT,
    contracts          VARCHAR[]
);

-- Rows that could not structurally form a bar. Only a pointer is kept: the
-- source file is immutable, so file + row number + reason is enough to trace
-- one back. These rows never entered `bars`.
CREATE TABLE IF NOT EXISTS rejected_rows (
    run_id       VARCHAR,
    source_file  VARCHAR,
    row_number   BIGINT,
    reason       VARCHAR
);

-- Bars that were valid but lost the (contract, frequency, ts) tie-break.
--
-- Deliberately NOT `rejected_rows`: these rows are not malformed, they were
-- displaced by a business rule, and conflating the two would make the reject
-- tally read as a data-quality figure when it is really a conflict-resolution
-- figure. Unlike a rejection, the full OHLCV is stored, because the first
-- question about a discarded-but-valid row is what it actually said.
CREATE TABLE IF NOT EXISTS superseded_bars (
    run_id            VARCHAR,
    reason            VARCHAR,   -- superseded_in_file | superseded_by_run
    winning_row_hash  VARCHAR,   -- the row that replaced this one
    contract          VARCHAR NOT NULL,
    root              VARCHAR,
    exchange          VARCHAR,
    frequency         VARCHAR NOT NULL,
    ts                TIMESTAMP WITH TIME ZONE NOT NULL,
    trading_date      DATE,
    open              DOUBLE,
    high              DOUBLE,
    low               DOUBLE,
    close             DOUBLE,
    volume            BIGINT,
    open_interest     BIGINT,
    source_file       VARCHAR,
    row_hash          VARCHAR,
    ingested_at       TIMESTAMP WITH TIME ZONE,
    superseded_at     TIMESTAMP WITH TIME ZONE
);

CREATE INDEX IF NOT EXISTS bars_contract_ts ON bars (contract, frequency, ts);
CREATE INDEX IF NOT EXISTS superseded_contract_ts ON superseded_bars (contract, frequency, ts);
