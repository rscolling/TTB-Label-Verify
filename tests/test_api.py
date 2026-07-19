"""L2: FastAPI endpoints via TestClient with the extractor dependency faked.

Covers the SPEC.md error-handling criteria end to end: happy path, bad file,
no label detected, extraction backend failure, oversized image handling, and
the R6 low-confidence -> review surfacing (trap 10) through the API.
No test here touches the network or needs an API key.
"""

from __future__ import annotations

import io

from PIL import Image

from app.extraction import ExtractionError
from app.models import ExtractedLabel
from tests.conftest import FakeExtractor

FIELD_ORDER = [
    "brand",
    "class_type",
    "abv",
    "net_contents",
    "producer",
    "origin_country",
    "government_warning",
]


def post_verify(client, image_bytes: bytes, form: dict[str, str], filename: str = "label.png"):
    return client.post(
        "/api/verify",
        files={"file": (filename, image_bytes, "image/png")},
        data=form,
    )


class TestHealth:
    def test_health_returns_ok(self, make_client):
        client = make_client(FakeExtractor())
        response = client.get("/api/health")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert "api_key_configured" in body
        assert "auth_required" in body

    def test_health_deep_includes_checks(self, make_client):
        client = make_client(FakeExtractor())
        response = client.get("/api/health", params={"deep": "true"})
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert "checks" in body
        assert "max_image_bytes" in body["checks"]


class TestVerifyHappyPath:
    def test_returns_all_seven_fields_with_full_shape(
        self, make_client, png_bytes, good_extraction, good_application
    ):
        client = make_client(FakeExtractor(good_extraction))
        response = post_verify(client, png_bytes, good_application)
        assert response.status_code == 200
        body = response.json()

        assert body["overall_status"] == "match"
        assert isinstance(body["processing_time_ms"], int)
        assert body["processing_time_ms"] >= 0

        assert list(body["fields"]) == FIELD_ORDER
        for name, field in body["fields"].items():
            assert field["verdict"] in ("match", "review", "mismatch", "na"), name
            for key in ("extracted", "expected", "similarity", "reason", "confidence"):
                assert key in field, f"{name}.{key}"
            assert field["reason"], name

    def test_trap1_brand_case_difference_matches_via_api(
        self, make_client, png_bytes, good_extraction, good_application
    ):
        """Trap 1 at L2: STONE'S THROW on the label vs Stone's Throw on the form."""
        client = make_client(FakeExtractor(good_extraction))
        body = post_verify(client, png_bytes, good_application).json()
        assert body["fields"]["brand"]["verdict"] == "match"
        assert body["fields"]["brand"]["extracted"] == "STONE'S THROW"

    def test_trap7a_unit_normalization_via_api(
        self, make_client, png_bytes, good_extraction, good_application
    ):
        """Trap 7 at L2: 750 mL on the label vs 75 cL on the form."""
        client = make_client(FakeExtractor(good_extraction))
        body = post_verify(client, png_bytes, good_application).json()
        assert body["fields"]["net_contents"]["verdict"] == "match"

    def test_trap8_domestic_origin_is_na_via_api(
        self, make_client, png_bytes, good_extraction, good_application
    ):
        """Trap 8 at L2: domestic product, no country on label -> na, not mismatch."""
        client = make_client(FakeExtractor(good_extraction))
        body = post_verify(client, png_bytes, good_application).json()
        assert body["fields"]["origin_country"]["verdict"] == "na"

    def test_mismatched_field_drives_overall_status(
        self, make_client, png_bytes, good_extraction, good_application
    ):
        good_extraction.alcohol_content = "40% ABV"  # trap 6 through the API
        client = make_client(FakeExtractor(good_extraction))
        body = post_verify(client, png_bytes, good_application).json()
        assert body["fields"]["abv"]["verdict"] == "mismatch"
        assert body["overall_status"] == "mismatch"

    def test_trap10_low_confidence_surfaces_as_review_via_api(
        self, make_client, png_bytes, good_extraction, good_application
    ):
        """Trap 10 at L2: a low-confidence read renders review, never a silent verdict."""
        good_extraction.confidence["brand"] = 0.3
        client = make_client(FakeExtractor(good_extraction))
        body = post_verify(client, png_bytes, good_application).json()
        assert body["fields"]["brand"]["verdict"] == "review"
        assert body["overall_status"] == "review"


class TestVerifyErrorPaths:
    def test_non_image_upload_is_friendly_400_and_never_calls_extractor(
        self, make_client, good_application
    ):
        fake = FakeExtractor()
        client = make_client(fake)
        response = post_verify(client, b"definitely not an image", good_application, "notes.txt")
        assert response.status_code == 400
        error = response.json()["error"]
        assert error["code"] == "bad_file"
        assert "JPG or PNG" in error["message"]
        assert fake.calls == 0  # rejected before the (paid) extraction call

    def test_no_label_detected_is_422_with_guidance(
        self, make_client, png_bytes, good_application
    ):
        client = make_client(FakeExtractor(ExtractedLabel(label_detected=False)))
        response = post_verify(client, png_bytes, good_application)
        assert response.status_code == 422
        error = response.json()["error"]
        assert error["code"] == "no_label"
        assert "couldn't read a label" in error["message"]

    def test_extractor_failure_is_clean_502_json(
        self, make_client, png_bytes, good_application
    ):
        client = make_client(
            FakeExtractor(error=ExtractionError("The label reading service is unavailable right now."))
        )
        response = post_verify(client, png_bytes, good_application)
        assert response.status_code == 502
        error = response.json()["error"]
        assert error["code"] == "extraction_failed"
        assert "unavailable" in error["message"]

    def test_missing_required_brand_field_is_validation_error(self, make_client, png_bytes):
        client = make_client(FakeExtractor())
        response = client.post(
            "/api/verify", files={"file": ("label.png", png_bytes, "image/png")}, data={}
        )
        assert response.status_code == 422  # FastAPI form validation


class TestOversizedImages:
    def test_oversized_image_is_processed_not_rejected(
        self, make_client, good_extraction, good_application
    ):
        """SPEC.md: oversized images are downscaled server-side, never rejected."""
        buffer = io.BytesIO()
        Image.new("RGB", (4000, 2500), "white").save(buffer, format="PNG")
        client = make_client(FakeExtractor(good_extraction))
        response = post_verify(client, buffer.getvalue(), good_application, "huge.png")
        assert response.status_code == 200
        assert response.json()["overall_status"] == "match"
