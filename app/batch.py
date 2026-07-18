"""Batch verification support: CSV manifest parsing + batch limits.

Design decision (R4): a real batch is 200-300 labels and each label has its
own application data, so a single set of shared form fields cannot be the
only mode. Two modes, one endpoint:

1. **Shared-fields mode** (no CSV): the form fields sent with the request
   apply to every uploaded label. Useful for spot-checking a production run
   of the same product.
2. **CSV-manifest mode**: the user uploads one CSV alongside the images with
   columns ``filename, brand, class_type, abv, net_contents, producer,
   origin_country, is_import`` — one row per label, matched by file name.
   This mirrors how the 47 compliance agents already work (a spreadsheet per
   review queue) and keeps the upload dead simple: pick the photos, pick the
   spreadsheet, click Check. No per-file JSON sidecars, no bespoke formats —
   an agent can build the CSV in Excel.

Why filename matching (not index alignment): browsers do not guarantee a
stable ordering for multi-file picks, and agents think in file names, not
positions. Manifest rows without a matching uploaded file are **ignored** —
deliberately, because the web UI re-sends the full manifest with every
sub-batch of images while reporting progress. A file without a manifest row
becomes a per-label error entry; the batch continues (per-label isolation).

Structural CSV problems (missing columns, empty/duplicate file names, a row
with no brand, an unreadable is_import flag) fail the whole request with a
friendly 400 *before* any API spend, so the agent can fix the spreadsheet
once instead of discovering 300 row-level errors after the fact.
"""

from __future__ import annotations

import csv
import io
import os

from app.models import ApplicationData

MAX_BATCH_SIZE = 300  # SPEC.md R4 targets 200-300 labels per batch.
DEFAULT_CONCURRENCY = 4

REQUIRED_COLUMNS = ("filename", "brand")
OPTIONAL_COLUMNS = ("class_type", "abv", "net_contents", "producer", "origin_country", "is_import")
ALL_COLUMNS = REQUIRED_COLUMNS + OPTIONAL_COLUMNS

_TRUE_VALUES = {"true", "yes", "y", "1"}
_FALSE_VALUES = {"false", "no", "n", "0", ""}


class ManifestError(Exception):
    """The uploaded CSV manifest cannot be used; message is user-facing."""


def batch_concurrency() -> int:
    """How many labels to process in parallel (env-tunable, min 1)."""
    try:
        value = int(os.environ.get("BATCH_CONCURRENCY", str(DEFAULT_CONCURRENCY)))
    except ValueError:
        return DEFAULT_CONCURRENCY
    return max(1, value)


def normalize_filename(name: str) -> str:
    """Case-insensitive basename: manifest rows match uploads by file name only."""
    trimmed = name.strip().replace("\\", "/")
    return trimmed.rsplit("/", 1)[-1].lower()


def _parse_is_import(raw: str, row_num: int) -> bool:
    value = raw.strip().lower()
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    raise ManifestError(
        f"Row {row_num} of the CSV has an unrecognized is_import value ({raw!r}). "
        "Please use true or false (or leave it blank for domestic products)."
    )


def parse_manifest(raw: bytes) -> dict[str, ApplicationData]:
    """Parse the CSV manifest into {normalized filename: ApplicationData}.

    Raises ManifestError with a friendly, actionable message on any
    structural problem. Never partially succeeds: the agent fixes the
    spreadsheet once, before any label is processed.
    """
    try:
        text = raw.decode("utf-8-sig")  # utf-8-sig: tolerate an Excel BOM
    except UnicodeDecodeError as exc:
        raise ManifestError(
            "We couldn't read that CSV file as text. Please save it as CSV "
            "(UTF-8) from your spreadsheet program and try again."
        ) from exc

    reader = csv.reader(io.StringIO(text))
    try:
        header_row = next(reader)
    except StopIteration:
        raise ManifestError("The CSV file is empty. Expected columns: " + ", ".join(ALL_COLUMNS) + ".") from None

    headers = [h.strip().lower() for h in header_row]
    for required in REQUIRED_COLUMNS:
        if required not in headers:
            raise ManifestError(
                f"The CSV is missing the '{required}' column. "
                "Expected columns: " + ", ".join(ALL_COLUMNS) + "."
            )
    index = {name: headers.index(name) for name in ALL_COLUMNS if name in headers}

    def cell(row: list[str], column: str) -> str:
        pos = index.get(column)
        if pos is None or pos >= len(row):
            return ""
        return row[pos].strip()

    applications: dict[str, ApplicationData] = {}
    row_count = 0
    for row_num, row in enumerate(reader, start=2):
        if not any(value.strip() for value in row):
            continue  # skip blank lines (Excel loves trailing ones)
        row_count += 1
        filename = cell(row, "filename")
        if not filename:
            raise ManifestError(f"Row {row_num} of the CSV has no filename. Every row needs the photo's file name.")
        key = normalize_filename(filename)
        if key in applications:
            raise ManifestError(
                f"The CSV lists '{filename}' more than once (row {row_num}). "
                "Each photo should appear exactly once."
            )
        brand = cell(row, "brand")
        if not brand:
            raise ManifestError(
                f"Row {row_num} of the CSV ('{filename}') has no brand. "
                "The brand name is required for every label."
            )
        applications[key] = ApplicationData(
            brand=brand,
            class_type=cell(row, "class_type") or None,
            abv=cell(row, "abv") or None,
            net_contents=cell(row, "net_contents") or None,
            producer=cell(row, "producer") or None,
            origin_country=cell(row, "origin_country") or None,
            is_import=_parse_is_import(cell(row, "is_import"), row_num),
        )

    if row_count == 0:
        raise ManifestError("The CSV has a header but no rows. Add one row per label photo.")
    return applications
