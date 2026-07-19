"""QA gate 5 — L2 file-type abuse against POST /api/ingest-form (WP7).

Adversarial uploads through the additive ingestion endpoint: oversized but
legal spreadsheets, renamed junk containers, binary garbage, extension/content
mismatches in both directions, 0-byte and extension-less files, and a >10MB
PDF. Every reject must be a friendly, actionable 4xx (never a stack trace or
the generic 500 shape), and structural rejects must never spend an extractor
call.

The form extractor is faked via the same DI seam the build agent exposed
(``get_form_extractor`` + ``dependency_overrides``) — zero network, zero API
key, zero persistence.
"""

from __future__ import annotations

import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from app.form_ingest import FormRow, MAX_FORM_ROWS
from app.main import app, get_form_extractor
from tests.conftest import FakeFormExtractor
from tests.qa._qa_worksheet_harness import xlsx_bytes


@pytest.fixture
def make_ingest_client():
    def _make(form_extractor) -> TestClient:
        app.dependency_overrides[get_form_extractor] = lambda: form_extractor
        return TestClient(app)

    yield _make
    app.dependency_overrides.clear()


def post_form(client: TestClient, name: str, raw: bytes,
              content_type: str = "application/octet-stream"):
    return client.post("/api/ingest-form", files={"file": (name, raw, content_type)})


def assert_friendly_400(response, *, fragment: str | None = None) -> str:
    """The reject is the documented error shape: 400 bad_form + prose message
    (no traceback text, not the last-resort internal_error 500)."""
    assert response.status_code == 400, (
        f"expected a friendly 400, got {response.status_code}: {response.text[:300]}"
    )
    error = response.json()["error"]
    assert error["code"] == "bad_form", f"unexpected error code {error['code']!r}"
    message = error["message"]
    assert "Traceback" not in message and "Error:" not in message, (
        f"stack-trace-ish text leaked into the user message: {message!r}"
    )
    if fragment is not None:
        assert fragment in message, f"{fragment!r} not in {message!r}"
    return message


# ---------------------------------------------------------------------------
# Structural rejects: friendly 400s, extractor untouched
# ---------------------------------------------------------------------------


class TestQa5StructuralRejects:
    def test_qa5_zero_byte_file_rejected_before_the_extractor(self, make_ingest_client):
        fake = FakeFormExtractor()
        client = make_ingest_client(fake)
        assert_friendly_400(post_form(client, "form.pdf", b""), fragment="empty")
        assert fake.calls == 0

    def test_qa5_whitespace_only_file_is_treated_as_empty(self, make_ingest_client):
        fake = FakeFormExtractor()
        client = make_ingest_client(fake)
        assert_friendly_400(post_form(client, "form.csv", b"   \n \t \n"), fragment="empty")
        assert fake.calls == 0

    def test_qa5_renamed_zip_of_junk_as_xlsx_is_a_friendly_400(self, make_ingest_client):
        """A real zip container full of junk entries wears the .xlsx name:
        openpyxl must not traceback through to the client."""
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("junk.txt", "not a workbook at all")
            archive.writestr("random/deeper.bin", b"\x00\x01\x02\x03" * 256)
        fake = FakeFormExtractor()
        client = make_ingest_client(fake)
        message = assert_friendly_400(
            post_form(client, "submittal.xlsx", buffer.getvalue()), fragment="Excel"
        )
        assert "openpyxl" not in message and "zipfile" not in message
        assert fake.calls == 0

    def test_qa5_binary_garbage_txt_is_a_friendly_400(self, make_ingest_client):
        # No magic bytes, .txt extension, not valid UTF-8 -> the decode error
        # must surface as the save-as-CSV guidance, not a UnicodeDecodeError.
        garbage = b"\x00\x01\x02\xfe\xff\xf0\x81" * 64
        fake = FakeFormExtractor()
        client = make_ingest_client(fake)
        message = assert_friendly_400(post_form(client, "notes.txt", garbage))
        assert "CSV" in message
        assert fake.calls == 0

    def test_qa5_extensionless_text_file_gets_the_what_is_this_message(self, make_ingest_client):
        fake = FakeFormExtractor()
        client = make_ingest_client(fake)
        message = assert_friendly_400(
            post_form(client, "submittal", b"filename,brand\na.png,Brand\n")
        )
        assert "couldn't tell" in message
        assert fake.calls == 0

    def test_qa5_legacy_doc_ole_file_is_named_in_the_reject(self, make_ingest_client):
        # Legacy .doc is an OLE container (no PK magic): the extension branch
        # must still name the format and the export path out.
        ole_magic = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64
        fake = FakeFormExtractor()
        client = make_ingest_client(fake)
        assert_friendly_400(post_form(client, "form.doc", ole_magic), fragment=".doc")
        assert fake.calls == 0

    def test_qa5_text_file_wearing_a_pdf_name_is_rejected_not_sent_to_the_llm(
        self, make_ingest_client
    ):
        """Content is the truth in the other direction too: plain text named
        .pdf has no PDF magic — it must NOT reach the (billed) extractor."""
        fake = FakeFormExtractor(rows=[FormRow(brand="X")])
        client = make_ingest_client(fake)
        assert_friendly_400(post_form(client, "form.pdf", b"filename,brand\na.png,B\n"))
        assert fake.calls == 0


# ---------------------------------------------------------------------------
# Size abuse: row caps and big payloads
# ---------------------------------------------------------------------------


class TestQa5SizeAbuse:
    def test_qa5_csv_with_10k_rows_is_capped_with_a_friendly_message(self, make_ingest_client):
        fake = FakeFormExtractor()
        client = make_ingest_client(fake)
        raw = b"filename,brand\n" + b"".join(
            b"photo-%d.png,Brand %d\n" % (i, i) for i in range(10_000)
        )
        message = assert_friendly_400(post_form(client, "big.csv", raw))
        assert str(MAX_FORM_ROWS) in message
        assert "batches" in message
        assert fake.calls == 0

    def test_qa5_xlsx_over_the_row_cap_is_capped_not_parsed_forever(self, make_ingest_client):
        rows: list[list[object]] = [["filename", "brand"]]
        rows.extend([f"photo-{i}.png", f"Brand {i}"] for i in range(MAX_FORM_ROWS + 1))
        fake = FakeFormExtractor()
        client = make_ingest_client(fake)
        message = assert_friendly_400(post_form(client, "big.xlsx", xlsx_bytes(rows)))
        assert str(MAX_FORM_ROWS) in message
        assert fake.calls == 0

    def test_qa5_xlsx_at_exactly_the_row_cap_parses(self, make_ingest_client):
        rows: list[list[object]] = [["filename", "brand"]]
        rows.extend([f"photo-{i}.png", f"Brand {i}"] for i in range(MAX_FORM_ROWS))
        client = make_ingest_client(FakeFormExtractor())
        response = post_form(client, "exact.xlsx", xlsx_bytes(rows))
        assert response.status_code == 200
        assert len(response.json()["rows"]) == MAX_FORM_ROWS

    def test_qa5_llm_form_over_the_row_cap_is_capped_after_extraction(self, make_ingest_client):
        fake = FakeFormExtractor(
            rows=[FormRow(brand=f"Brand {i}") for i in range(MAX_FORM_ROWS + 1)]
        )
        client = make_ingest_client(fake)
        message = assert_friendly_400(post_form(client, "form.pdf", b"%PDF-1.4 x"))
        assert str(MAX_FORM_ROWS) in message

    def test_qa5_xlsx_with_giant_cells_round_trips_intact(self, make_ingest_client):
        # Large-but-legal: a 30k-char producer cell must survive verbatim.
        giant = "Very Long Distilling Co., " + "x" * 30_000
        raw = xlsx_bytes([["filename", "brand", "producer"], ["a.png", "Brand", giant]])
        client = make_ingest_client(FakeFormExtractor())
        response = post_form(client, "giant-cell.xlsx", raw)
        assert response.status_code == 200
        assert response.json()["rows"][0]["producer"] == giant

    def test_qa5_pdf_over_10mb_is_handled_gracefully(self, make_ingest_client, monkeypatch):
        """Size cap (QA P0-1): oversize forms get a friendly 413 before spend."""
        monkeypatch.setenv("MAX_FORM_BYTES", str(5 * 1024 * 1024))
        fake = FakeFormExtractor(rows=[FormRow(filename="a.png", brand="Big Form")])
        client = make_ingest_client(fake)
        big_pdf = b"%PDF-1.4\n" + b"0" * (11 * 1024 * 1024)
        response = post_form(client, "huge.pdf", big_pdf, "application/pdf")
        assert response.status_code == 413
        assert response.json()["error"]["code"] == "payload_too_large"
        assert fake.calls == 0


# ---------------------------------------------------------------------------
# Extension/content mismatches: magic bytes win, routing is pinned
# ---------------------------------------------------------------------------


class TestQa5MismatchRouting:
    def test_qa5_pdf_renamed_csv_routes_to_the_pdf_extractor(self, make_ingest_client):
        fake = FakeFormExtractor(rows=[FormRow(filename="a.png", brand="From PDF")])
        client = make_ingest_client(fake)
        response = post_form(client, "renamed.csv", b"%PDF-1.4 secretly a pdf", "text/csv")
        assert response.status_code == 200
        assert fake.kinds == ["pdf"]
        assert response.json()["source_kind"] == "pdf-llm"

    def test_qa5_xlsx_renamed_csv_routes_to_the_xlsx_parser(self, make_ingest_client):
        fake = FakeFormExtractor()
        client = make_ingest_client(fake)
        raw = xlsx_bytes([["filename", "brand"], ["a.png", "Sheet Brand"]])
        response = post_form(client, "renamed.csv", raw, "text/csv")
        assert response.status_code == 200
        assert response.json()["source_kind"] == "xlsx"
        assert response.json()["rows"][0]["brand"] == "Sheet Brand"
        assert fake.calls == 0

    def test_qa5_png_renamed_xlsx_routes_to_the_image_extractor_by_magic(
        self, make_ingest_client, png_bytes
    ):
        """Pins actual behavior: PNG magic beats the .xlsx name, so the file is
        treated as a PHOTO of the form (image-llm), not rejected. NOTE: the
        module docstring in app/form_ingest.py claims a PNG renamed .xlsx "is
        rejected with a friendly message" — the implementation instead routes
        it to the LLM path. Behavior is graceful (content-is-truth, consistent
        with the PDF case) so this test pins it; the stale docstring is filed
        as a LOW doc finding in the QA report."""
        fake = FakeFormExtractor(rows=[FormRow(brand="From Photo")])
        client = make_ingest_client(fake)
        response = post_form(client, "renamed.xlsx", png_bytes)
        assert response.status_code == 200
        assert fake.kinds == ["image"]
        assert response.json()["source_kind"] == "image-llm"

    def test_qa5_docx_magic_wins_over_a_csv_name(self, make_ingest_client):
        # A docx renamed .csv is a PK container whose extension is .csv — the
        # zip branch accepts it as xlsx-candidate and openpyxl then rejects it
        # with the friendly Excel message. Whatever the wording, it must be a
        # 400 with no extractor spend.
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("word/document.xml", "<w:document/>")
            archive.writestr("[Content_Types].xml", "<Types/>")
        fake = FakeFormExtractor()
        client = make_ingest_client(fake)
        assert_friendly_400(post_form(client, "report.csv", buffer.getvalue()))
        assert fake.calls == 0
