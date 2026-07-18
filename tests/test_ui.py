"""L2 — UI page and static asset serving (WP2).

Locks: GET / serves the single-page UI, the page references its assets, the
assets themselves serve with sensible content types, and adding the UI did
not disturb the API routes.
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

    def test_page_has_the_core_controls(self, client):
        html = client.get("/").text
        assert "Check This Label" in html
        assert 'type="file"' in html
        # Every form field the API accepts is present on the page.
        for field_id in ("brand", "class_type", "abv", "net_contents", "producer", "origin_country", "is_import"):
            assert f'id="{field_id}"' in html, field_id

    def test_page_is_plain_language_no_jargon(self, client):
        # R5: visible text avoids jargon; "ABV" appears only as an input hint example.
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

    def test_unknown_asset_is_404(self, client):
        assert client.get("/static/nope.js").status_code == 404


class TestApiUnchanged:
    def test_health_still_works(self, client):
        response = client.get("/api/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
