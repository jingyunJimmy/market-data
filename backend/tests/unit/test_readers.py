"""File-format adapters and path expansion.

Readers understand formats, not the market-data schema, so what matters here is
that an unknown extension fails with an actionable message and that the path
expansion the CLI relies on resolves files, directories and globs the same way.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from market_data.ingestion.readers import (
    ROW_NUM_COL,
    SOURCE_COL,
    UnsupportedFileError,
    expand_paths,
    reader_for,
)

# --------------------------------------------------------------------------- #
# Picking a reader
#
# Dispatch is on the extension alone, so the only two things that can go wrong
# are a file whose case does not match the table, and a format nobody handles.
# --------------------------------------------------------------------------- #


def test_reader_is_chosen_by_extension_case_insensitively(tmp_path):
    """An upper-case suffix is the same format: a file named .CSV must not be
    read as an unsupported one. Asserting on `extensions` rather than the class
    keeps the test tied to what dispatch actually matches on."""
    assert reader_for(tmp_path / "a.CSV").extensions == (".csv", ".txt")
    assert reader_for(tmp_path / "a.parquet").extensions == (".parquet", ".pq")


def test_an_unknown_extension_names_the_formats_that_are_supported(tmp_path):
    """The message is the whole value of the error: it has to say what to do."""
    with pytest.raises(UnsupportedFileError) as excinfo:
        reader_for(tmp_path / "bars.xlsx")
    message = str(excinfo.value)
    assert ".xlsx" in message
    assert ".csv" in message and ".parquet" in message


# --------------------------------------------------------------------------- #
# Provenance
#
# The reader is the only layer that still knows where a row came from. If it
# does not record that here, no later stage can reconstruct it.
# --------------------------------------------------------------------------- #


def test_every_raw_row_carries_its_origin(tmp_path):
    """Reject reporting points at "file X, row N", so both must be attached."""
    f = tmp_path / "bars.csv"
    f.write_text("close\n1.0\n2.0\n")

    df = reader_for(f).read(f)

    # Absolute and resolved, so the same file reached by two different relative
    # paths reports one origin.
    assert df[SOURCE_COL].to_list() == [str(f.resolve())] * 2
    # Numbering counts data rows, so N matches what a user sees in a spreadsheet
    # once the header line is accounted for.
    assert df[ROW_NUM_COL].to_list() == [1, 2]  # 1-based, header excluded


# --------------------------------------------------------------------------- #
# Path expansion
#
# The CLI accepts files, directories and shell-style globs interchangeably, and
# every one of them has to collapse into the same thing: a sorted, duplicate-free
# list of real files. Sorted because ingestion order decides the last-write-wins
# tie-break in normalize, so it must not depend on filesystem order.
# --------------------------------------------------------------------------- #


def test_expand_paths_walks_a_directory_recursively(tmp_path):
    """A directory means every file under it, not just its top level."""
    (tmp_path / "daily").mkdir()
    a = tmp_path / "daily" / "a.csv"
    b = tmp_path / "b.csv"
    a.write_text("x\n")
    b.write_text("x\n")

    assert expand_paths([tmp_path]) == sorted([a, b])


def test_expand_paths_deduplicates_overlapping_inputs(tmp_path):
    """A directory and a file inside it must not ingest that file twice."""
    f = tmp_path / "a.csv"
    f.write_text("x\n")

    assert expand_paths([tmp_path, f]) == [f]


def test_expand_paths_resolves_a_glob(tmp_path, monkeypatch):
    """A pattern is expanded by us, not left to the shell.

    It matters when the shell never got the chance to expand it -- a quoted
    argument, or a call from the API rather than a terminal. The parquet file
    is here to prove the pattern is honoured rather than the directory walked.
    """
    keep = tmp_path / "a.csv"
    skip = tmp_path / "b.parquet"
    keep.write_text("x\n")
    skip.write_text("x\n")
    monkeypatch.chdir(tmp_path)

    assert expand_paths([Path("*.csv")]) == [Path("a.csv")]


def test_expand_paths_ignores_a_path_that_does_not_exist(tmp_path):
    """The CLI turns an empty result into its own error; this must not raise."""
    assert expand_paths([tmp_path / "nope.csv"]) == []
