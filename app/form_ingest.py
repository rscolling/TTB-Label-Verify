"""Submittal-form ingestion (WP7): format detection + parsing into normalized rows.

The submittal form's input format is unknown — agents receive whatever the
applicant sent. This module is the dispatch layer:

- ``.csv`` / ``.tsv`` / ``.txt``  -> deterministic delimited-text parser
- ``.xlsx``                        -> deterministic openpyxl parser (first sheet)
- ``.pdf`` / ``.png`` / ``.jpg``   -> ``ClaudeFormExtractor`` (LLM document
  extraction — perception only; every verdict downstream stays deterministic)

Detection uses BOTH the file extension and the content's magic bytes: a PDF
renamed ``.csv`` still routes to the PDF path, and a PNG renamed ``.xlsx`` is
rejected with a friendly message instead of an openpyxl traceback.

Every format normalizes to the same row shape (the batch-manifest columns) plus
a ``source_kind`` tag and a warnings list (e.g. "no filename column — rows will
be matched to photos by order"). Header names are matched case-insensitively
with aliases ("class type" == "class_type", "alcohol content" == "abv",
"import" == "is_import"), because real-world spreadsheets never agree on
column names.

Nothing here persists anything (R8) and nothing here renders a verdict — the
LLM transcribes what the form says; the rules engine still judges.
"""

from __future__ import annotations

import base64
import csv
import io
import os
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.extraction import BadImageError, DEFAULT_MODEL, ExtractionError, prepare_image

# Reuse the manifest's is_import vocabulary so the two parsers can never drift.
from app.batch import _FALSE_VALUES, _TRUE_VALUES

PREVIEW_SOURCE_KINDS = ("csv", "tsv", "xlsx", "pdf-llm", "image-llm")

MAX_FORM_ROWS = 300  # mirrors MAX_BATCH_SIZE; a form can't address more photos


class FormIngestError(Exception):
    """The uploaded form cannot be used; message is user-facing."""


@dataclass
class FormRow:
    """One application row read off the submittal form (normalized)."""

    filename: str | None = None
    brand: str | None = None
    class_type: str | None = None
    abv: str | None = None
    net_contents: str | None = None
    producer: str | None = None
    origin_country: str | None = None
    is_import: bool = False
    row_notes: str | None = None


@dataclass
class IngestResult:
    rows: list[FormRow]
    source_kind: str
    warnings: list[str] = field(default_factory=list)


class FormExtractor(Protocol):
    """Anything that can transcribe application rows from a PDF or photo."""

    def extract_rows(self, raw: bytes, kind: str) -> list[FormRow]:  # pragma: no cover - protocol
        ...


# ---------------------------------------------------------------------------
# Format detection (extension AND magic bytes)
# ---------------------------------------------------------------------------

_UNSUPPORTED_EXTENSIONS = {
    "doc", "docx", "odt", "rtf", "pages",
    "xls", "ods", "numbers",
    "ppt", "pptx",
    "heic", "gif", "bmp", "tiff", "tif",
    "zip", "html", "htm", "json", "xml", "md",
}


def _extension(filename: str) -> str:
    base = filename.strip().replace("\\", "/").rsplit("/", 1)[-1]
    if "." not in base:
        return ""
    return base.rsplit(".", 1)[-1].lower()


def detect_format(filename: str, raw: bytes) -> str:
    """Classify the upload: csv | tsv | txt | xlsx | pdf | image.

    Binary magic bytes win over the extension (a PDF named .csv is a PDF);
    text formats fall back to the extension because text has no magic.
    Raises FormIngestError with a friendly message for anything unusable.
    """
    ext = _extension(filename)
    if not raw or not raw.strip():
        raise FormIngestError(
            "The form file is empty. Export the submittal form again and re-add it."
        )

    # Binary magic first — content is the truth, whatever the file is named.
    if raw[:5] == b"%PDF-":
        return "pdf"
    if raw[:8] == b"\x89PNG\r\n\x1a\n" or raw[:3] == b"\xff\xd8\xff":
        return "image"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image"
    if raw[:4] == b"PK\x03\x04":
        # Office files are all zip containers; only .xlsx is parseable here.
        if ext in ("docx", "doc", "pptx", "ppt", "odt", "ods"):
            raise FormIngestError(
                f"We can't read .{ext} files — export the form as PDF, CSV, or Excel "
                "(.xlsx) and add it again."
            )
        return "xlsx"

    # No binary magic: treat as text, routed by extension.
    if ext in ("csv", "tsv", "txt"):
        return ext
    if ext == "xlsx":
        raise FormIngestError(
            "That .xlsx file doesn't look like a real Excel workbook. "
            "Re-export it from your spreadsheet program (or save it as CSV) and try again."
        )
    if ext in _UNSUPPORTED_EXTENSIONS:
        raise FormIngestError(
            f"We can't read .{ext} files — export the form as CSV, Excel (.xlsx), or PDF, "
            "or add a photo of it, and try again."
        )
    raise FormIngestError(
        "We couldn't tell what kind of file that is. The submittal form can be a CSV, "
        "an Excel file (.xlsx), a PDF, or a photo (JPG/PNG)."
    )


# ---------------------------------------------------------------------------
# Header aliasing (shared by the CSV/TSV and XLSX parsers)
# ---------------------------------------------------------------------------

_HEADER_ALIASES: dict[str, str] = {
    # filename
    "filename": "filename", "file_name": "filename", "file": "filename",
    "photo": "filename", "photo_name": "filename", "image": "filename",
    "image_name": "filename", "label_image": "filename",
    # brand
    "brand": "brand", "brand_name": "brand",
    # class/type
    "class_type": "class_type", "class": "class_type", "type": "class_type",
    "class_and_type": "class_type", "class_or_type": "class_type",
    "kind_of_drink": "class_type", "designation": "class_type",
    # alcohol content
    "abv": "abv", "alcohol_content": "abv", "alcohol": "abv", "alc": "abv",
    "alcohol_by_volume": "abv",
    # net contents
    "net_contents": "net_contents", "net_content": "net_contents",
    "contents": "net_contents", "volume": "net_contents", "size": "net_contents",
    # producer
    "producer": "producer", "producer_name": "producer", "bottler": "producer",
    "producer_and_address": "producer", "producer_address": "producer",
    # origin
    "origin_country": "origin_country", "country_of_origin": "origin_country",
    "country": "origin_country", "origin": "origin_country",
    # import flag
    "is_import": "is_import", "import": "is_import", "imported": "is_import",
    "is_imported": "is_import",
    # notes
    "notes": "row_notes", "note": "row_notes", "row_notes": "row_notes",
    "comments": "row_notes",
}

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _normalize_header(header: str) -> str:
    return _NON_ALNUM.sub("_", header.strip().lower()).strip("_")


def _parse_is_import(raw: str, row_num: int) -> bool:
    value = raw.strip().lower()
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    raise FormIngestError(
        f"Row {row_num} of the form has an unrecognized import value ({raw!r}). "
        "Please use true or false (or leave it blank for domestic products)."
    )


def _rows_from_table(header_cells: list[str], data_rows: list[list[str]]) -> tuple[list[FormRow], list[str]]:
    """Shared table→rows logic for the deterministic parsers (CSV/TSV/XLSX)."""
    warnings: list[str] = []
    columns: dict[str, int] = {}
    unrecognized: list[str] = []
    for position, header in enumerate(header_cells):
        if not header.strip():
            continue
        canonical = _HEADER_ALIASES.get(_normalize_header(header))
        if canonical is None:
            unrecognized.append(header.strip())
        elif canonical not in columns:  # first occurrence wins
            columns[canonical] = position

    if "brand" not in columns:
        raise FormIngestError(
            "We couldn't find a brand column in the form. Expected column names like: "
            "filename, brand, class_type, abv, net_contents, producer, origin_country, is_import."
        )
    if unrecognized:
        warnings.append(
            "Ignored unrecognized column(s): " + ", ".join(unrecognized[:6]) + "."
        )

    def cell(row: list[str], column: str) -> str:
        pos = columns.get(column)
        if pos is None or pos >= len(row):
            return ""
        return row[pos].strip()

    rows: list[FormRow] = []
    for row_num, raw_row in enumerate(data_rows, start=2):
        if not any(value.strip() for value in raw_row):
            continue  # blank line (Excel loves trailing ones)
        if len(rows) >= MAX_FORM_ROWS:
            raise FormIngestError(
                f"The form has more than {MAX_FORM_ROWS} rows — that's more photos than "
                "one scan can check. Split it into smaller batches."
            )
        rows.append(
            FormRow(
                filename=cell(raw_row, "filename") or None,
                brand=cell(raw_row, "brand") or None,
                class_type=cell(raw_row, "class_type") or None,
                abv=cell(raw_row, "abv") or None,
                net_contents=cell(raw_row, "net_contents") or None,
                producer=cell(raw_row, "producer") or None,
                origin_country=cell(raw_row, "origin_country") or None,
                is_import=_parse_is_import(cell(raw_row, "is_import"), row_num),
                row_notes=cell(raw_row, "row_notes") or None,
            )
        )

    if not rows:
        raise FormIngestError("The form has a header but no rows. Add one row per label photo.")

    if "filename" not in columns:
        warnings.append(
            "No filename column — rows will be matched to photos by order."
        )
    else:
        unnamed = sum(1 for row in rows if row.filename is None)
        if unnamed == len(rows):
            warnings.append("No filename column — rows will be matched to photos by order.")
        elif unnamed:
            warnings.append(
                f"{unnamed} row(s) don't name a photo — they can only be matched to the "
                "remaining photos by order."
            )
    return rows, warnings


def _decode_text(raw: bytes) -> str:
    try:
        return raw.decode("utf-8-sig")  # utf-8-sig: tolerate an Excel BOM
    except UnicodeDecodeError as exc:
        raise FormIngestError(
            "We couldn't read that file as text. Please save the form as CSV (UTF-8) "
            "from your spreadsheet program and try again."
        ) from exc


def parse_delimited(raw: bytes, delimiter: str) -> tuple[list[FormRow], list[str]]:
    text = _decode_text(raw)
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    table = list(reader)
    if not table:
        raise FormIngestError("The form file is empty. Add a header row and one row per label photo.")
    return _rows_from_table(table[0], table[1:])


def _xlsx_cell_text(value: Any) -> str:
    """Excel cells carry typed values; normalize them to the CSV string forms."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def parse_xlsx(raw: bytes) -> tuple[list[FormRow], list[str]]:
    try:
        import openpyxl

        workbook = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    except Exception as exc:  # zipfile/openpyxl raise a zoo of types on corrupt input
        raise FormIngestError(
            "We couldn't open that Excel file. Re-export it as .xlsx (or save it as CSV) "
            "and try again."
        ) from exc
    try:
        sheet = workbook.worksheets[0]  # first sheet, same as the CSV contract
        table = [[_xlsx_cell_text(cell) for cell in row] for row in sheet.iter_rows(values_only=True)]
    finally:
        workbook.close()
    # Skip leading fully-blank rows before the header (common in hand-made sheets).
    while table and not any(cell.strip() for cell in table[0]):
        table.pop(0)
    if not table:
        raise FormIngestError("The Excel sheet is empty. Add a header row and one row per label photo.")
    return _rows_from_table(table[0], table[1:])


# ---------------------------------------------------------------------------
# LLM document extraction (perception only — the rules engine still judges)
# ---------------------------------------------------------------------------

_ROW_FIELD_SCHEMA: dict[str, Any] = {"type": ["string", "null"]}

_FORM_TOOL: dict[str, Any] = {
    "name": "record_form_rows",
    "description": "Record the application rows transcribed from an alcohol-label submittal form.",
    "input_schema": {
        "type": "object",
        "properties": {
            "rows": {
                "type": "array",
                "description": "One entry per application/label row on the form.",
                "items": {
                    "type": "object",
                    "properties": {
                        "filename": {
                            **_ROW_FIELD_SCHEMA,
                            "description": "The label photo's file name if the form states one; null otherwise.",
                        },
                        "brand": {**_ROW_FIELD_SCHEMA, "description": "Brand name exactly as written."},
                        "class_type": {
                            **_ROW_FIELD_SCHEMA,
                            "description": "Class/type designation (e.g. 'Kentucky Straight Bourbon Whiskey').",
                        },
                        "abv": {
                            **_ROW_FIELD_SCHEMA,
                            "description": "Alcohol content exactly as written (e.g. '45%', '90 Proof').",
                        },
                        "net_contents": {
                            **_ROW_FIELD_SCHEMA,
                            "description": "Net contents exactly as written (e.g. '750 mL').",
                        },
                        "producer": {
                            **_ROW_FIELD_SCHEMA,
                            "description": "Producer/bottler name and address as written.",
                        },
                        "origin_country": {
                            **_ROW_FIELD_SCHEMA,
                            "description": "Country of origin if stated; null otherwise.",
                        },
                        "is_import": {
                            "type": ["boolean", "null"],
                            "description": "True only if the form marks the product as an import; null when the form doesn't say.",
                        },
                        "row_notes": {
                            **_ROW_FIELD_SCHEMA,
                            "description": "Anything else the form states for this row that doesn't fit the fields above.",
                        },
                    },
                    "required": [],
                },
            },
        },
        "required": ["rows"],
    },
}

_FORM_SYSTEM_PROMPT = (
    "You transcribe alcohol-label submittal forms for a compliance workflow. "
    "The document lists one or more label applications; record one output row "
    "per application/label row on the form. Transcribe exactly what the form "
    "says — never invent, complete, or normalize values. Use null for anything "
    "the form does not state. Do not guess file names, countries, or import "
    "status; a missing value is null, not a best guess."
)


class ClaudeFormExtractor:
    """Forced tool-use document extraction for PDFs and photos of the form.

    Mirrors ClaudeExtractor's engineering: lazy client (no key needed at
    import or construction), EXTRACTION_MODEL env knob, one retry for
    transient failures / malformed payloads, immediate failure on permanent
    4xx. Perception only — no verdicts come from here.
    """

    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        self._api_key = api_key
        self._model = model or os.environ.get("EXTRACTION_MODEL", DEFAULT_MODEL)
        self._client: Any = None  # lazy: constructing must not require a key

    def _get_client(self) -> Any:
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic(api_key=self._api_key or os.environ.get("ANTHROPIC_API_KEY"))
        return self._client

    def _document_block(self, raw: bytes, kind: str) -> dict[str, Any]:
        if kind == "pdf":
            return {
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": base64.b64encode(raw).decode("ascii"),
                },
            }
        # Photo of the form: validate + downscale exactly like a label photo.
        try:
            prepared, media_type = prepare_image(raw)
        except BadImageError as exc:
            raise FormIngestError(
                "We couldn't read that photo of the form. Try a straight-on, well-lit "
                "photo, or export the form as PDF or CSV."
            ) from exc
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": base64.b64encode(prepared).decode("ascii"),
            },
        }

    def extract_rows(self, raw: bytes, kind: str) -> list[FormRow]:
        """Transcribe the form in one call. Retries once on transient failure."""
        document = self._document_block(raw, kind)
        import anthropic

        last_error: Exception | None = None
        for _ in range(2):  # one retry, same policy as ClaudeExtractor
            try:
                response = self._get_client().messages.create(
                    model=self._model,
                    max_tokens=4000,
                    system=_FORM_SYSTEM_PROMPT,
                    tools=[_FORM_TOOL],
                    tool_choice={"type": "tool", "name": "record_form_rows"},
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                document,
                                {"type": "text", "text": "Transcribe this submittal form's application rows."},
                            ],
                        }
                    ],
                )
                return self._parse_response(response)
            except (anthropic.APIConnectionError, TimeoutError) as exc:
                last_error = exc  # transient: retry once
            except ExtractionError as exc:
                last_error = exc  # malformed tool payload: a fresh call usually parses
            except anthropic.APIStatusError as exc:
                if exc.status_code >= 500 or exc.status_code == 429:
                    last_error = exc  # server-side/rate-limit: retry once
                else:
                    # 4xx is permanent — retrying a bad request only doubles the failure.
                    raise ExtractionError("The form reading service rejected the request.") from exc
        if isinstance(last_error, ExtractionError):
            raise ExtractionError(str(last_error)) from last_error
        raise ExtractionError("The form reading service is unavailable right now.") from last_error

    @staticmethod
    def _parse_response(response: Any) -> list[FormRow]:
        tool_use = next((block for block in response.content if block.type == "tool_use"), None)
        if tool_use is None or not isinstance(tool_use.input, dict):
            raise ExtractionError("The form reading service returned an unexpected response.")
        raw_rows = tool_use.input.get("rows")
        if not isinstance(raw_rows, list):
            raise ExtractionError("The form reading service returned an unexpected response.")

        def text(value: Any) -> str | None:
            if value is None:
                return None
            if not isinstance(value, str):
                value = str(value)
            value = value.strip()
            return value or None

        rows: list[FormRow] = []
        for item in raw_rows:
            if not isinstance(item, dict):
                raise ExtractionError("The form reading service returned an unexpected response.")
            rows.append(
                FormRow(
                    filename=text(item.get("filename")),
                    brand=text(item.get("brand")),
                    class_type=text(item.get("class_type")),
                    abv=text(item.get("abv")),
                    net_contents=text(item.get("net_contents")),
                    producer=text(item.get("producer")),
                    origin_country=text(item.get("origin_country")),
                    is_import=bool(item.get("is_import")),  # null -> False
                    row_notes=text(item.get("row_notes")),
                )
            )
        return rows


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _row_warnings(rows: list[FormRow]) -> list[str]:
    """Warnings that apply to any source's rows (LLM or deterministic)."""
    warnings: list[str] = []
    missing_brand = [i + 1 for i, row in enumerate(rows) if not (row.brand or "").strip()]
    if missing_brand:
        listed = ", ".join(str(n) for n in missing_brand[:6])
        warnings.append(
            f"Row(s) {listed} have no brand name — every application needs one; "
            "fix the form before scanning."
        )
    named = [row.filename for row in rows if row.filename]
    seen: set[str] = set()
    duplicates: list[str] = []
    for name in named:
        key = name.strip().lower()
        if key in seen and name not in duplicates:
            duplicates.append(name)
        seen.add(key)
    if duplicates:
        warnings.append(
            "The form lists the same photo more than once: " + ", ".join(duplicates[:4]) + "."
        )
    return warnings


def ingest_form(filename: str, raw: bytes, form_extractor: FormExtractor) -> IngestResult:
    """Detect the form's format, parse it, and return normalized rows.

    Raises FormIngestError (user-fixable input problems) or ExtractionError
    (the LLM document-extraction backend failed).
    """
    kind = detect_format(filename, raw)

    if kind in ("csv", "tsv", "txt"):
        if kind == "tsv":
            delimiter: str = "\t"
        elif kind == "csv":
            delimiter = ","
        else:  # .txt: sniff the header line — tabs win, otherwise commas
            first_line = _decode_text(raw).splitlines()[0] if raw.strip() else ""
            delimiter = "\t" if "\t" in first_line else ","
        rows, warnings = parse_delimited(raw, delimiter)
        source_kind = "tsv" if delimiter == "\t" else "csv"
    elif kind == "xlsx":
        rows, warnings = parse_xlsx(raw)
        source_kind = "xlsx"
    else:  # pdf | image -> LLM document extraction
        rows = form_extractor.extract_rows(raw, kind)
        if not rows:
            raise FormIngestError(
                "We couldn't find any application rows in that form. Check that it's the "
                "submittal form (one row per label), or export it as CSV and try again."
            )
        if len(rows) > MAX_FORM_ROWS:
            raise FormIngestError(
                f"The form has more than {MAX_FORM_ROWS} rows — that's more photos than "
                "one scan can check. Split it into smaller batches."
            )
        warnings = []
        if all(row.filename is None for row in rows):
            warnings.append("No filename column — rows will be matched to photos by order.")
        elif any(row.filename is None for row in rows):
            count = sum(1 for row in rows if row.filename is None)
            warnings.append(
                f"{count} row(s) don't name a photo — they can only be matched to the "
                "remaining photos by order."
            )
        source_kind = "pdf-llm" if kind == "pdf" else "image-llm"

    warnings.extend(_row_warnings(rows))
    return IngestResult(rows=rows, source_kind=source_kind, warnings=warnings)
