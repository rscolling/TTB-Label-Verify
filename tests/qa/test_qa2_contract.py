"""QA gate 2 — API response-contract lock for the single-label endpoint.

WP3 concurrently adds a BATCH endpoint + eval harness. The single-label
POST /api/verify response shape is consumed by app.js and MUST NOT drift.
These offline TestClient locks fail loudly if a field is added, removed, or
renamed in the success or error body.
"""

from __future__ import annotations

from tests.conftest import FakeExtractor

SUCCESS_TOP_KEYS = {"overall_status", "processing_time_ms", "fields"}
FIELD_KEYS = {
    "field",
    "verdict",
    "extracted",
    "expected",
    "similarity",
    "reason",
    "detail",
    "confidence",
}
EXPECTED_FIELD_ORDER = [
    "brand",
    "class_type",
    "abv",
    "net_contents",
    "producer",
    "origin_country",
    "government_warning",
]


def _post(client, image_bytes, form):
    return client.post(
        "/api/verify",
        files={"file": ("label.png", image_bytes, "image/png")},
        data=form,
    )


class TestSuccessShapeLocked:
    def test_top_level_and_per_field_keys_are_exact(
        self, make_client, png_bytes, good_extraction, good_application
    ):
        client = make_client(FakeExtractor(good_extraction))
        body = _post(client, png_bytes, good_application).json()
        assert set(body) == SUCCESS_TOP_KEYS
        assert isinstance(body["processing_time_ms"], int)
        assert body["overall_status"] in {"match", "review", "mismatch"}
        for name, field in body["fields"].items():
            assert set(field) == FIELD_KEYS, name
            assert field["verdict"] in {"match", "review", "mismatch", "na"}, name

    def test_field_set_and_order_is_stable(
        self, make_client, png_bytes, good_extraction, good_application
    ):
        # app.js iterates Object.keys(data.fields) for row order; JSON objects
        # preserve insertion order, so this locks the on-screen row sequence.
        client = make_client(FakeExtractor(good_extraction))
        body = _post(client, png_bytes, good_application).json()
        assert list(body["fields"].keys()) == EXPECTED_FIELD_ORDER


class TestErrorShapeLocked:
    def test_bad_file_error_shape(self, make_client, good_application):
        client = make_client(FakeExtractor())
        body = _post(client, b"not an image", good_application).json()
        assert set(body) == {"error"}
        assert set(body["error"]) == {"code", "message"}
        assert body["error"]["code"] == "bad_file"

    def test_no_label_error_shape(
        self, make_client, png_bytes, good_application
    ):
        from app.models import ExtractedLabel

        client = make_client(FakeExtractor(ExtractedLabel(label_detected=False)))
        response = _post(client, png_bytes, good_application)
        assert response.status_code == 422
        body = response.json()
        assert body["error"]["code"] == "no_label"
