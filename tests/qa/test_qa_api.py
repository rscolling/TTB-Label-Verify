"""QA gate 1 — adversarial API-level cases (mocked extractor, offline).

Locks: multipart edge cases, blank optional fields, import flow end-to-end,
error body shape, all-None extraction degrades to verdicts (not a crash).
"""

from __future__ import annotations

import io

from PIL import Image

from app.models import ExtractedLabel
from tests.conftest import FakeExtractor


def post_verify(client, image_bytes: bytes, form: dict[str, str], filename="label.png", content_type="image/png"):
    return client.post(
        "/api/verify",
        files={"file": (filename, image_bytes, content_type)},
        data=form,
    )


class TestMultipartEdges:
    def test_missing_file_part_is_422(self, make_client, good_application):
        client = make_client(FakeExtractor())
        response = client.post("/api/verify", data=good_application)
        assert response.status_code == 422

    def test_zero_byte_file_is_friendly_400(self, make_client, good_application):
        client = make_client(FakeExtractor())
        response = post_verify(client, b"", good_application)
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "bad_file"

    def test_wrong_declared_content_type_with_valid_png_still_works(
        self, make_client, png_bytes, good_extraction, good_application
    ):
        # Content sniffing must win over the (untrusted) declared content type.
        client = make_client(FakeExtractor(good_extraction))
        response = post_verify(
            client, png_bytes, good_application, filename="label.bin", content_type="application/octet-stream"
        )
        assert response.status_code == 200

    def test_html_masquerading_as_png_is_friendly_400(self, make_client, good_application):
        client = make_client(FakeExtractor())
        response = post_verify(client, b"<html><body>hi</body></html>", good_application)
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "bad_file"

    def test_very_large_image_is_downscaled_and_processed(
        self, make_client, good_extraction, good_application
    ):
        # Noise compresses poorly -> a multi-MB upload exercising the downscale path.
        noise = Image.effect_noise((3600, 2400), 64).convert("RGB")
        buffer = io.BytesIO()
        noise.save(buffer, format="PNG")
        client = make_client(FakeExtractor(good_extraction))
        response = post_verify(client, buffer.getvalue(), good_application, "huge.png")
        assert response.status_code == 200
        assert response.json()["overall_status"] == "match"


class TestBlankFormFields:
    def test_blank_optional_fields_render_na_not_mismatch(
        self, make_client, png_bytes, good_extraction
    ):
        # HTML forms submit empty strings for untouched inputs; those must mean
        # "not provided" (na), never a mismatch against the extracted value.
        client = make_client(FakeExtractor(good_extraction))
        form = {
            "brand": "Stone's Throw",
            "class_type": "",
            "abv": "",
            "net_contents": "",
            "producer": "",
            "origin_country": "",
            "is_import": "false",
        }
        body = post_verify(client, png_bytes, form).json()
        for field in ("class_type", "abv", "net_contents", "producer"):
            assert body["fields"][field]["verdict"] == "na", field
        assert body["fields"]["brand"]["verdict"] == "match"


class TestImportFlow:
    def test_import_with_matching_country_end_to_end(
        self, make_client, png_bytes, good_extraction, good_application
    ):
        good_extraction.origin_country = "Product of France"
        good_application.update({"is_import": "true", "origin_country": "France"})
        client = make_client(FakeExtractor(good_extraction))
        body = post_verify(client, png_bytes, good_application).json()
        assert body["fields"]["origin_country"]["verdict"] == "match"

    def test_import_missing_country_on_label_fails_end_to_end(
        self, make_client, png_bytes, good_extraction, good_application
    ):
        good_extraction.origin_country = None
        good_application.update({"is_import": "true", "origin_country": "France"})
        client = make_client(FakeExtractor(good_extraction))
        body = post_verify(client, png_bytes, good_application).json()
        assert body["fields"]["origin_country"]["verdict"] == "mismatch"
        assert body["overall_status"] == "mismatch"


class TestDegradedExtraction:
    def test_all_fields_none_yields_verdicts_not_a_crash(
        self, make_client, png_bytes, good_application
    ):
        # Extractor found a label but read nothing: every provided application
        # field must come back mismatch/review with a reason — never a 500.
        empty = ExtractedLabel(label_detected=True, confidence={})
        client = make_client(FakeExtractor(empty))
        response = post_verify(client, png_bytes, good_application)
        assert response.status_code == 200
        body = response.json()
        assert body["overall_status"] == "mismatch"
        for name, field in body["fields"].items():
            assert field["verdict"] in ("match", "review", "mismatch", "na"), name
            assert field["reason"], name

    def test_error_body_shape_is_stable_contract(self, make_client, good_application):
        # WP2's UI will key off error.code/error.message — lock the shape.
        client = make_client(FakeExtractor())
        response = post_verify(client, b"not an image", good_application)
        body = response.json()
        assert set(body) == {"error"}
        assert set(body["error"]) == {"code", "message"}
