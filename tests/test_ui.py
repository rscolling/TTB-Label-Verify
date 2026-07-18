"""L2 — UI page and static asset serving (WP2, reshaped by WP5).

Locks: GET / serves the unified worksheet UI (dropzone + submittal CSV slot +
scan button + worksheet table), the page references its assets, the assets
serve with sensible content types, and the UI redesign did not disturb the
API routes. The old two-tab layout and the "enter application details" form
are gone by design (WP5) — this file locks their absence.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


class TestIndexPage:
    def test_root_serves_html(self, client):
        response = client.get("/")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")

    def test_page_references_its_assets(self, client):
        html = client.get("/").text
        assert "/static/styles.css" in html
        assert "/static/app.js" in html
        # The old split batch script is gone — one unified flow, one script.
        assert "/static/batch.js" not in html

    def test_page_has_the_core_controls(self, client):
        html = client.get("/").text
        # Drag-and-drop photo upload (multi-file), the submittal CSV slot,
        # one scan button, and the worksheet table.
        assert 'id="file-input"' in html
        assert "multiple" in html
        assert 'id="csv-input"' in html
        assert 'id="scan-button"' in html
        assert "Scan Labels" in html
        assert 'id="worksheet"' in html
        assert 'id="worksheet-body"' in html

    def test_application_details_form_is_gone(self, client):
        # WP5: application data arrives ONLY via the submittal CSV — there is
        # no typed application-details form and no mode tabs anymore.
        html = client.get("/").text
        for removed_id in ("brand", "class_type", "abv", "tab-single", "tab-batch", "batch_brand"):
            assert f'id="{removed_id}"' not in html, removed_id

    def test_worksheet_has_the_owner_required_columns(self, client):
        html = client.get("/").text
        for column in ("Serial", "Scanned at", "Time", "Photo", "Brand name", "Health warning", "Score", "Result"):
            assert column in html, column

    def test_blank_submittal_template_control_is_present(self, client):
        # Audit drift fix: with the typed form gone, non-technical users need
        # a starting point for the 8-column submittal CSV — a downloadable
        # blank template next to the upload slot, plus a plain-language hint.
        html = client.get("/").text
        assert 'id="template-download"' in html
        assert "Download a blank submittal form (CSV)" in html
        assert "must match the photo" in html

    def test_page_is_plain_language_no_jargon(self, client):
        # R5: visible text avoids jargon.
        html = client.get("/").text
        assert "Alcohol content" in html

    def test_results_region_is_aria_live(self, client):
        html = client.get("/").text
        assert 'aria-live="polite"' in html


class TestStaticAssets:
    def test_stylesheet_serves(self, client):
        response = client.get("/static/styles.css")
        assert response.status_code == 200
        assert "text/css" in response.headers["content-type"]

    def test_script_serves(self, client):
        response = client.get("/static/app.js")
        assert response.status_code == 200
        assert "javascript" in response.headers["content-type"]

    def test_removed_batch_script_is_404(self, client):
        assert client.get("/static/batch.js").status_code == 404

    def test_unknown_asset_is_404(self, client):
        assert client.get("/static/nope.js").status_code == 404


class TestApiUnchanged:
    def test_health_still_works(self, client):
        response = client.get("/api/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
