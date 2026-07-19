"""L2: upload size caps, optional API key, rate limit (QA P0)."""

from __future__ import annotations

import os

import pytest

from tests.conftest import FakeExtractor


def post_verify(client, image_bytes: bytes, form: dict[str, str], filename: str = "label.png", headers=None):
    return client.post(
        "/api/verify",
        files={"file": (filename, image_bytes, "image/png")},
        data=form,
        headers=headers or {},
    )


class TestUploadLimits:
    def test_oversized_image_is_413(self, make_client, good_application, monkeypatch):
        monkeypatch.setenv("MAX_IMAGE_BYTES", "1000")
        # Re-import limits are read at call time via max_image_bytes()
        client = make_client(FakeExtractor())
        big = b"\x89PNG\r\n\x1a\n" + b"0" * 2000
        response = post_verify(client, big, good_application)
        assert response.status_code == 413
        body = response.json()
        assert body["error"]["code"] == "payload_too_large"
        assert "limit" in body["error"]["message"].lower()

    def test_oversized_form_is_413(self, make_client, monkeypatch):
        from app.main import app, get_form_extractor
        from tests.conftest import FakeFormExtractor

        monkeypatch.setenv("MAX_FORM_BYTES", "500")
        app.dependency_overrides[get_form_extractor] = lambda: FakeFormExtractor()
        client = make_client(FakeExtractor())
        big = b"%PDF-1.4\n" + b"0" * 2000
        response = client.post(
            "/api/ingest-form",
            files={"file": ("huge.pdf", big, "application/pdf")},
        )
        assert response.status_code == 413
        assert response.json()["error"]["code"] == "payload_too_large"
        app.dependency_overrides.pop(get_form_extractor, None)


class TestApiKeyAuth:
    def test_verify_requires_key_when_configured(
        self, make_client, png_bytes, good_extraction, good_application, monkeypatch
    ):
        monkeypatch.setenv("VERIFY_API_KEY", "secret-test-key")
        client = make_client(FakeExtractor(good_extraction))
        denied = post_verify(client, png_bytes, good_application)
        assert denied.status_code == 401
        assert denied.json()["error"]["code"] == "unauthorized"

        ok = post_verify(
            client, png_bytes, good_application, headers={"X-API-Key": "secret-test-key"}
        )
        assert ok.status_code == 200

    def test_health_stays_open_when_key_configured(self, make_client, monkeypatch):
        monkeypatch.setenv("VERIFY_API_KEY", "secret-test-key")
        client = make_client(FakeExtractor())
        assert client.get("/api/health").status_code == 200
