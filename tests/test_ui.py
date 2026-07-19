"""L2 — UI page and static asset serving (WP2, reshaped by WP5).

Locks: GET / serves the unified worksheet UI (photo dropzone + submittal CSV
dropzone + scan button + worksheet table), the page references its assets, the
assets serve with sensible content types, and the UI redesign did not disturb
the API routes. The old two-tab layout and the "enter application details" form
are gone by design (WP5) — this file locks their absence. Owner feedback:
step 2 mirrors step 1 — a drag-and-drop zone, not a bare file input.
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
        # Drag-and-drop photo upload (multi-file), the submittal form slot,
        # one Run button, and the worksheet table.
        assert 'id="file-input"' in html
        assert "multiple" in html
        assert 'id="csv-input"' in html
        assert 'id="scan-button"' in html
        assert ">Run</button>" in html
        assert 'id="worksheet"' in html
        assert 'id="worksheet-body"' in html

    def test_run_button_lives_inside_the_form_card_not_below_the_cards(self, client):
        # Owner direction (WP7): the Run button is THE action, placed inside
        # the step-2 card directly below the form dropzone — no separate
        # centered submit row competing with it.
        html = client.get("/").text
        assert 'class="submit-row"' not in html
        assert "Scan Labels" not in html
        card_two = html.split('id="csv-heading"')[1].split("</section>")[0]
        assert 'id="scan-button"' in card_two
        assert 'class="run-row"' in card_two

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

    def test_submittal_form_step_is_a_dropzone_like_the_photos(self, client):
        # Owner feedback: step 2 must have the same look and feel as step 1 —
        # a dashed drag-and-drop zone with a big "choose from your computer"
        # button, not a text-heavy block around a bare file input.
        html = client.get("/").text
        assert 'id="csv-dropzone"' in html
        assert "Drag the submittal form here" in html
        assert "Choose form from your computer" in html
        # The hidden input keeps its id (source of truth for selection) and
        # now accepts every supported form format (WP7).
        assert 'id="csv-input"' in html
        for accepted in (".csv", ".tsv", ".xlsx", ".pdf", ".png", ".jpg"):
            assert accepted in html, accepted
        # Selected state: status line, warnings, preview + clear control live
        # inside the zone.
        assert 'id="csv-dropzone-selected"' in html
        assert 'id="csv-status"' in html
        assert 'id="csv-clear"' in html
        assert 'id="form-warnings"' in html
        assert 'id="ingest-preview"' in html
        assert "Show what was read" in html
        assert 'id="match-notice"' in html

    def test_submittal_hint_is_present(self, client):
        # Plain-language guidance in the form drop zone (the owner removed
        # the blank-template download button; the hint stays) — now naming
        # every accepted format plus the order-matching rule.
        html = client.get("/").text
        assert 'id="template-download"' not in html
        assert "CSV, Excel (.xlsx), PDF, or a photo of the form" in html
        assert "matched to photos in order" in html

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
        body = response.json()
        assert body["status"] == "ok"
        assert "api_key_configured" in body
