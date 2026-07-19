"""L2: POST /api/ingest-form — the additive WP7 endpoint, via TestClient with
the form extractor faked through the same DI pattern as the label extractor
(Depends + dependency_overrides). No network, no API key, no persistence.
"""

from __future__ import annotations

import io

import openpyxl
import pytest
from fastapi.testclient import TestClient

from app.extraction import ExtractionError
from app.form_ingest import FormExtractor, FormRow
from app.main import app, get_form_extractor
from tests.conftest import FakeFormExtractor


@pytest.fixture
def make_ingest_client():
    """Build a TestClient with the form-extractor dependency overridden."""

    def _make(form_extractor: FormExtractor) -> TestClient:
        app.dependency_overrides[get_form_extractor] = lambda: form_extractor
        return TestClient(app)

    yield _make
    app.dependency_overrides.clear()


def post_form(client: TestClient, name: str, raw: bytes, content_type: str = "application/octet-stream"):
    return client.post("/api/ingest-form", files={"file": (name, raw, content_type)})


def xlsx_bytes(rows: list[list[object]]) -> bytes:
    workbook = openpyxl.Workbook()
    for row in rows:
        workbook.active.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


LLM_ROWS = [
    FormRow(filename=None, brand="Copper Hollow", class_type="Bourbon", abv="45%"),
    FormRow(filename=None, brand="Juniper Gate", is_import=True),
]


class TestIngestFormHappyPaths:
    def test_csv_returns_rows_source_kind_and_warnings(self, make_ingest_client):
        client = make_ingest_client(FakeFormExtractor())
        raw = b"filename,brand,is_import\na.png,Copper Hollow,false\n"
        body = post_form(client, "submittal.csv", raw, "text/csv").json()
        assert body["source_kind"] == "csv"
        assert body["warnings"] == []
        assert body["rows"] == [
            {
                "filename": "a.png", "brand": "Copper Hollow", "class_type": None,
                "abv": None, "net_contents": None, "producer": None,
                "origin_country": None, "is_import": False, "row_notes": None,
            }
        ]

    def test_tsv_returns_tsv_source_kind(self, make_ingest_client):
        client = make_ingest_client(FakeFormExtractor())
        body = post_form(client, "submittal.tsv", b"filename\tbrand\na.png\tBrand\n").json()
        assert body["source_kind"] == "tsv"
        assert body["rows"][0]["brand"] == "Brand"

    def test_xlsx_parses_first_sheet(self, make_ingest_client):
        client = make_ingest_client(FakeFormExtractor())
        raw = xlsx_bytes([["filename", "brand"], ["a.png", "Copper Hollow"]])
        body = post_form(client, "submittal.xlsx", raw).json()
        assert body["source_kind"] == "xlsx"
        assert body["rows"][0]["brand"] == "Copper Hollow"

    def test_pdf_goes_through_the_fake_extractor(self, make_ingest_client):
        fake = FakeFormExtractor(rows=LLM_ROWS)
        client = make_ingest_client(fake)
        body = post_form(client, "submittal.pdf", b"%PDF-1.4 body", "application/pdf").json()
        assert fake.kinds == ["pdf"]
        assert body["source_kind"] == "pdf-llm"
        assert [row["brand"] for row in body["rows"]] == ["Copper Hollow", "Juniper Gate"]
        assert any("matched to photos by order" in warning for warning in body["warnings"])

    def test_photo_of_the_form_goes_through_the_fake_extractor(self, make_ingest_client, png_bytes):
        fake = FakeFormExtractor(rows=LLM_ROWS)
        client = make_ingest_client(fake)
        body = post_form(client, "form-photo.png", png_bytes, "image/png").json()
        assert fake.kinds == ["image"]
        assert body["source_kind"] == "image-llm"

    def test_csv_never_calls_the_llm_extractor(self, make_ingest_client):
        fake = FakeFormExtractor(error=AssertionError("must not be called"))
        client = make_ingest_client(fake)
        response = post_form(client, "submittal.csv", b"filename,brand\na.png,Brand\n")
        assert response.status_code == 200
        assert fake.calls == 0


class TestIngestFormErrorPaths:
    def test_unsupported_docx_is_a_friendly_400(self, make_ingest_client):
        client = make_ingest_client(FakeFormExtractor())
        response = post_form(client, "form.docx", b"PK\x03\x04word")
        assert response.status_code == 400
        error = response.json()["error"]
        assert error["code"] == "bad_form"
        assert ".docx" in error["message"]

    def test_empty_file_is_a_400(self, make_ingest_client):
        client = make_ingest_client(FakeFormExtractor())
        response = post_form(client, "form.csv", b"")
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "bad_form"

    def test_corrupt_xlsx_is_a_400(self, make_ingest_client):
        client = make_ingest_client(FakeFormExtractor())
        response = post_form(client, "form.xlsx", b"PK\x03\x04not-a-workbook")
        assert response.status_code == 400
        assert "Excel" in response.json()["error"]["message"]

    def test_structural_csv_problem_is_a_400(self, make_ingest_client):
        client = make_ingest_client(FakeFormExtractor())
        response = post_form(client, "form.csv", b"filename,color\na.png,red\n")
        assert response.status_code == 400
        assert "brand" in response.json()["error"]["message"]

    def test_extractor_failure_is_a_friendly_502(self, make_ingest_client):
        client = make_ingest_client(
            FakeFormExtractor(error=ExtractionError("The form reading service is unavailable right now."))
        )
        response = post_form(client, "form.pdf", b"%PDF-1.4", "application/pdf")
        assert response.status_code == 502
        error = response.json()["error"]
        assert error["code"] == "form_extraction_failed"
        assert "try again" in error["message"]

    def test_llm_form_with_no_rows_is_a_400(self, make_ingest_client):
        client = make_ingest_client(FakeFormExtractor(rows=[]))
        response = post_form(client, "form.pdf", b"%PDF-1.4", "application/pdf")
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "bad_form"
