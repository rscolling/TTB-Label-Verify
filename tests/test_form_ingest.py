"""L1/L2: submittal-form ingestion (WP7) — format detection, the deterministic
CSV/TSV/XLSX parsers with header aliases, and the ClaudeFormExtractor seam.

The Anthropic client is faked at the `_client` seam; nothing here needs a
network connection or an ANTHROPIC_API_KEY.
"""

from __future__ import annotations

import io
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx
import openpyxl
import pytest
from PIL import Image

from app.extraction import DEFAULT_MODEL, ExtractionError
from app.form_ingest import (
    MAX_FORM_ROWS,
    ClaudeFormExtractor,
    FormIngestError,
    FormRow,
    detect_format,
    ingest_form,
)
from tests.conftest import FakeFormExtractor

CSV_HEADER = "filename,brand,class_type,abv,net_contents,producer,origin_country,is_import"


def png_bytes() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (64, 64), "white").save(buffer, format="PNG")
    return buffer.getvalue()


def xlsx_bytes(rows: list[list[Any]]) -> bytes:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def raising_extractor() -> FakeFormExtractor:
    """Deterministic paths must never touch the LLM extractor."""
    return FakeFormExtractor(error=AssertionError("form extractor must not be called"))


class TestDetectFormat:
    def test_extensions_route_text_formats(self):
        assert detect_format("form.csv", b"filename,brand\n") == "csv"
        assert detect_format("form.tsv", b"filename\tbrand\n") == "tsv"
        assert detect_format("form.txt", b"filename,brand\n") == "txt"

    def test_magic_bytes_win_over_extension(self):
        # A PDF renamed .csv is still a PDF; a PNG renamed .xlsx is a photo.
        assert detect_format("form.csv", b"%PDF-1.7 rest") == "pdf"
        assert detect_format("form.xlsx", png_bytes()) == "image"
        assert detect_format("scan.pdf", b"\xff\xd8\xff\xe0jpegdata") == "image"

    def test_zip_magic_with_xlsx_extension_is_xlsx(self):
        assert detect_format("form.xlsx", b"PK\x03\x04rest-of-zip") == "xlsx"

    def test_docx_gets_a_friendly_unsupported_message(self):
        with pytest.raises(FormIngestError, match=r"\.docx"):
            detect_format("form.docx", b"PK\x03\x04word-zip")

    def test_unknown_extension_is_rejected_with_guidance(self):
        with pytest.raises(FormIngestError, match="CSV"):
            detect_format("form.wat", b"some text")

    def test_empty_file_is_rejected(self):
        with pytest.raises(FormIngestError, match="empty"):
            detect_format("form.csv", b"   \n")

    def test_text_masquerading_as_xlsx_is_rejected(self):
        with pytest.raises(FormIngestError, match="Excel"):
            detect_format("form.xlsx", b"filename,brand\na.png,Brand\n")


class TestDelimitedParsing:
    def test_csv_happy_path(self):
        raw = (
            f"{CSV_HEADER}\n"
            'a.png,"Stone\'s Throw",Bourbon,45%,750 mL,"Distiller, KY",,false\n'
            "b.png,Juniper Gate,Gin,47%,700 mL,London Distillery,England,true\n"
        ).encode("utf-8")
        result = ingest_form("form.csv", raw, raising_extractor())
        assert result.source_kind == "csv"
        assert result.warnings == []
        assert len(result.rows) == 2
        first, second = result.rows
        assert first.filename == "a.png"
        assert first.brand == "Stone's Throw"
        assert first.producer == "Distiller, KY"
        assert first.is_import is False
        assert second.origin_country == "England"
        assert second.is_import is True

    def test_tsv_uses_tab_delimiter(self):
        raw = ("filename\tbrand\ta.png\n".replace("a.png", "") + "a.png\tBrand One\n").encode()
        raw = b"filename\tbrand\na.png\tBrand One\n"
        result = ingest_form("form.tsv", raw, raising_extractor())
        assert result.source_kind == "tsv"
        assert result.rows[0].brand == "Brand One"

    def test_txt_sniffs_tabs_then_commas(self):
        tabbed = ingest_form("form.txt", b"filename\tbrand\na.png\tBrand\n", raising_extractor())
        assert tabbed.source_kind == "tsv"
        comma = ingest_form("form.txt", b"filename,brand\na.png,Brand\n", raising_extractor())
        assert comma.source_kind == "csv"

    def test_header_aliases_map_to_canonical_columns(self):
        raw = (
            "Photo,Brand Name,Class Type,Alcohol Content,Net Contents,"
            "Producer,Country of Origin,Import\n"
            "a.png,Copper Hollow,Bourbon,45%,750 mL,Copper Hollow Co.,,yes\n"
        ).encode("utf-8")
        result = ingest_form("form.csv", raw, raising_extractor())
        row = result.rows[0]
        assert row.filename == "a.png"
        assert row.brand == "Copper Hollow"
        assert row.class_type == "Bourbon"
        assert row.abv == "45%"
        assert row.net_contents == "750 mL"
        assert row.origin_country is None
        assert row.is_import is True

    def test_missing_brand_column_is_an_error(self):
        with pytest.raises(FormIngestError, match="brand column"):
            ingest_form("form.csv", b"filename,color\na.png,red\n", raising_extractor())

    def test_no_filename_column_warns_about_order_matching(self):
        result = ingest_form("form.csv", b"brand,abv\nBrand One,45%\n", raising_extractor())
        assert result.rows[0].filename is None
        assert any("matched to photos by order" in warning for warning in result.warnings)

    def test_some_missing_filenames_warn(self):
        raw = b"filename,brand\na.png,Brand One\n,Brand Two\n"
        result = ingest_form("form.csv", raw, raising_extractor())
        assert any("don't name a photo" in warning for warning in result.warnings)

    def test_header_only_is_an_error(self):
        with pytest.raises(FormIngestError, match="no rows"):
            ingest_form("form.csv", b"filename,brand\n", raising_extractor())

    def test_unrecognized_is_import_value_is_an_error(self):
        raw = f"{CSV_HEADER}\na.png,Brand,,,,,,oui\n".encode()
        with pytest.raises(FormIngestError, match="import value"):
            ingest_form("form.csv", raw, raising_extractor())

    def test_missing_brand_cell_warns_but_parses(self):
        raw = b"filename,brand\na.png,\n"
        result = ingest_form("form.csv", raw, raising_extractor())
        assert result.rows[0].brand is None
        assert any("no brand name" in warning for warning in result.warnings)

    def test_duplicate_filenames_warn(self):
        raw = b"filename,brand\na.png,One\nA.PNG,Two\n"
        result = ingest_form("form.csv", raw, raising_extractor())
        assert any("more than once" in warning for warning in result.warnings)

    def test_unrecognized_columns_are_warned_and_ignored(self):
        raw = b"filename,brand,shoe_size\na.png,Brand,44\n"
        result = ingest_form("form.csv", raw, raising_extractor())
        assert any("shoe_size" in warning for warning in result.warnings)

    def test_row_cap_is_enforced(self):
        body = "".join(f"l{i}.png,Brand {i}\n" for i in range(MAX_FORM_ROWS + 1))
        with pytest.raises(FormIngestError, match=str(MAX_FORM_ROWS)):
            ingest_form("form.csv", f"filename,brand\n{body}".encode(), raising_extractor())

    def test_non_utf8_text_is_a_friendly_error(self):
        with pytest.raises(FormIngestError, match="CSV"):
            ingest_form("form.csv", b"filename,brand\n\xff\xfe\x9c\n", raising_extractor())


class TestXlsxParsing:
    def test_xlsx_happy_path_with_typed_cells(self):
        raw = xlsx_bytes([
            ["filename", "brand", "abv", "net_contents", "is_import"],
            ["a.png", "Copper Hollow", 45.0, 750, True],
            ["b.png", "Juniper Gate", "47%", "700 mL", False],
        ])
        result = ingest_form("form.xlsx", raw, raising_extractor())
        assert result.source_kind == "xlsx"
        assert result.rows[0].abv == "45"           # 45.0 -> "45", not "45.0"
        assert result.rows[0].net_contents == "750"
        assert result.rows[0].is_import is True
        assert result.rows[1].is_import is False

    def test_xlsx_header_aliases_apply(self):
        raw = xlsx_bytes([
            ["Photo", "Brand", "Class Type", "Alcohol Content"],
            ["a.png", "Brand One", "Bourbon", "45%"],
        ])
        row = ingest_form("form.xlsx", raw, raising_extractor()).rows[0]
        assert row.class_type == "Bourbon"
        assert row.abv == "45%"

    def test_xlsx_leading_blank_rows_are_skipped(self):
        raw = xlsx_bytes([
            [None, None],
            ["filename", "brand"],
            ["a.png", "Brand One"],
        ])
        result = ingest_form("form.xlsx", raw, raising_extractor())
        assert result.rows[0].brand == "Brand One"

    def test_corrupt_xlsx_is_a_friendly_error(self):
        with pytest.raises(FormIngestError, match="Excel"):
            ingest_form("form.xlsx", b"PK\x03\x04this is not a real zip", raising_extractor())

    def test_xlsx_without_brand_column_is_an_error(self):
        raw = xlsx_bytes([["filename", "color"], ["a.png", "red"]])
        with pytest.raises(FormIngestError, match="brand column"):
            ingest_form("form.xlsx", raw, raising_extractor())


class TestLlmDispatch:
    def test_pdf_routes_to_the_form_extractor_with_kind_pdf(self):
        fake = FakeFormExtractor(rows=[FormRow(filename="a.png", brand="Brand")])
        result = ingest_form("form.pdf", b"%PDF-1.4 body", fake)
        assert fake.kinds == ["pdf"]
        assert result.source_kind == "pdf-llm"
        assert result.rows[0].brand == "Brand"

    def test_image_routes_with_kind_image(self):
        fake = FakeFormExtractor(rows=[FormRow(filename="a.png", brand="Brand")])
        result = ingest_form("photo-of-form.jpg", png_bytes(), fake)
        assert fake.kinds == ["image"]
        assert result.source_kind == "image-llm"

    def test_llm_rows_without_filenames_warn_about_order_matching(self):
        fake = FakeFormExtractor(rows=[FormRow(brand="One"), FormRow(brand="Two")])
        result = ingest_form("form.pdf", b"%PDF-1.4", fake)
        assert any("matched to photos by order" in warning for warning in result.warnings)

    def test_llm_empty_form_is_a_friendly_error(self):
        with pytest.raises(FormIngestError, match="application rows"):
            ingest_form("form.pdf", b"%PDF-1.4", FakeFormExtractor(rows=[]))

    def test_csv_never_touches_the_form_extractor(self):
        fake = raising_extractor()
        ingest_form("form.csv", b"filename,brand\na.png,Brand\n", fake)
        assert fake.calls == 0


# ---------------------------------------------------------------------------
# ClaudeFormExtractor — faked Anthropic client at the `_client` seam
# ---------------------------------------------------------------------------


def tool_use_response(input_data: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(content=[SimpleNamespace(type="tool_use", input=input_data)])


def connection_error() -> anthropic.APIConnectionError:
    return anthropic.APIConnectionError(
        request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    )


def status_error(code: int) -> anthropic.APIStatusError:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(code, request=request)
    return anthropic.APIStatusError("boom", response=response, body=None)


class FakeMessages:
    def __init__(self, results: list[Any]) -> None:
        self.results = list(results)
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def make_extractor(results: list[Any]) -> tuple[ClaudeFormExtractor, FakeMessages]:
    extractor = ClaudeFormExtractor()
    messages = FakeMessages(results)
    extractor._client = SimpleNamespace(messages=messages)  # inject at the lazy seam
    return extractor, messages


GOOD_PAYLOAD = {
    "rows": [
        {
            "filename": "a.png",
            "brand": "Copper Hollow",
            "class_type": "Bourbon",
            "abv": "45%",
            "net_contents": "750 mL",
            "producer": "Copper Hollow Co., Bardstown, KY",
            "origin_country": None,
            "is_import": None,
            "row_notes": None,
        },
        {"filename": None, "brand": "Juniper Gate", "is_import": True},
    ]
}


class TestClaudeFormExtractor:
    def test_constructs_without_api_key(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        extractor = ClaudeFormExtractor()
        assert extractor._client is None  # client is only built on first use

    def test_extraction_model_env_var_overrides_default(self, monkeypatch):
        monkeypatch.setenv("EXTRACTION_MODEL", "claude-haiku-4-5-20251001")
        assert ClaudeFormExtractor()._model == "claude-haiku-4-5-20251001"
        monkeypatch.delenv("EXTRACTION_MODEL")
        assert ClaudeFormExtractor()._model == DEFAULT_MODEL

    def test_single_call_with_forced_tool_and_pdf_document_block(self):
        extractor, messages = make_extractor([tool_use_response(GOOD_PAYLOAD)])
        rows = extractor.extract_rows(b"%PDF-1.4 body", "pdf")

        assert len(messages.calls) == 1
        call = messages.calls[0]
        assert call["tool_choice"] == {"type": "tool", "name": "record_form_rows"}
        assert call["tools"][0]["name"] == "record_form_rows"
        block = call["messages"][0]["content"][0]
        assert block["type"] == "document"
        assert block["source"]["media_type"] == "application/pdf"
        assert rows[0].brand == "Copper Hollow"

    def test_image_kind_sends_a_downscaled_image_block(self):
        extractor, messages = make_extractor([tool_use_response(GOOD_PAYLOAD)])
        extractor.extract_rows(png_bytes(), "image")
        block = messages.calls[0]["messages"][0]["content"][0]
        assert block["type"] == "image"
        assert block["source"]["media_type"] == "image/jpeg"  # prepare_image output

    def test_payload_maps_to_form_rows_with_null_handling(self):
        extractor, _ = make_extractor([tool_use_response(GOOD_PAYLOAD)])
        rows = extractor.extract_rows(b"%PDF-1.4", "pdf")
        assert rows[0].is_import is False        # null -> False
        assert rows[0].origin_country is None
        assert rows[1].filename is None
        assert rows[1].is_import is True

    def test_whitespace_values_become_none(self):
        payload = {"rows": [{"brand": "   ", "filename": "a.png"}]}
        extractor, _ = make_extractor([tool_use_response(payload)])
        assert extractor.extract_rows(b"%PDF-1.4", "pdf")[0].brand is None

    def test_bad_image_raises_before_any_api_call(self):
        extractor, messages = make_extractor([])
        with pytest.raises(FormIngestError):
            extractor.extract_rows(b"not an image", "image")
        assert messages.calls == []

    def test_malformed_payload_recovers_on_retry(self):
        bad = SimpleNamespace(content=[SimpleNamespace(type="text", text="hi")])
        extractor, messages = make_extractor([bad, tool_use_response(GOOD_PAYLOAD)])
        rows = extractor.extract_rows(b"%PDF-1.4", "pdf")
        assert len(messages.calls) == 2
        assert rows[0].brand == "Copper Hollow"

    def test_persistently_malformed_payload_is_extraction_error(self):
        bad = tool_use_response({"rows": "not-a-list"})
        extractor, messages = make_extractor([bad, bad])
        with pytest.raises(ExtractionError, match="unexpected response"):
            extractor.extract_rows(b"%PDF-1.4", "pdf")
        assert len(messages.calls) == 2

    def test_transient_failure_retried_once_then_succeeds(self):
        extractor, messages = make_extractor([connection_error(), tool_use_response(GOOD_PAYLOAD)])
        assert extractor.extract_rows(b"%PDF-1.4", "pdf")[0].brand == "Copper Hollow"
        assert len(messages.calls) == 2

    def test_persistent_failure_raises_after_one_retry(self):
        extractor, messages = make_extractor([connection_error(), connection_error()])
        with pytest.raises(ExtractionError, match="unavailable"):
            extractor.extract_rows(b"%PDF-1.4", "pdf")
        assert len(messages.calls) == 2

    def test_server_side_500_gets_the_single_retry(self):
        extractor, messages = make_extractor([status_error(502), tool_use_response(GOOD_PAYLOAD)])
        assert extractor.extract_rows(b"%PDF-1.4", "pdf")[0].brand == "Copper Hollow"
        assert len(messages.calls) == 2

    def test_permanent_4xx_fails_immediately_without_retry(self):
        extractor, messages = make_extractor([status_error(400)])
        with pytest.raises(ExtractionError, match="rejected"):
            extractor.extract_rows(b"%PDF-1.4", "pdf")
        assert len(messages.calls) == 1
