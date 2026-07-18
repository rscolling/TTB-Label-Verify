"""L4 (mocked) — browser-driven E2E of the single-label UI.

Boots the real FastAPI app on a loopback port inside this process, with the
extractor dependency overridden to a switchable fake — no API key, no network
beyond localhost. Drives headless Chromium via Playwright.

These tests are marked ``e2e`` and skip cleanly (never fail) when Playwright
or its Chromium build is unavailable on the machine.
"""

from __future__ import annotations

import socket
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

playwright_api = pytest.importorskip(
    "playwright.sync_api", reason="playwright is not installed"
)

import uvicorn

from app.extraction import ExtractionError
from app.main import app, get_extractor
from app.models import ExtractedLabel
from tests.conftest import HIGH_CONFIDENCE, FakeExtractor
from app.rules.warning import CANONICAL_WARNING

pytestmark = pytest.mark.e2e

REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_LABEL = REPO_ROOT / "eval" / "labels" / "01-bourbon-clean.png"

GOOD_EXTRACTION = ExtractedLabel(
    brand="STONE'S THROW",
    class_type="Kentucky Straight Bourbon Whiskey",
    alcohol_content="45% Alc./Vol.",
    net_contents="750 mL",
    producer="Blue Ridge Distilling Co., 12 Main Street, Asheville, NC 28801",
    origin_country=None,
    government_warning=CANONICAL_WARNING,
    warning_prefix_appears_bold=True,
    confidence=dict(HIGH_CONFIDENCE),
    label_detected=True,
)


class SwitchableExtractor:
    """One extractor instance whose behavior each test can reconfigure."""

    def __init__(self) -> None:
        self.delegate = FakeExtractor(GOOD_EXTRACTION)

    def extract(self, image_bytes: bytes) -> ExtractedLabel:
        return self.delegate.extract(image_bytes)


@pytest.fixture(scope="module")
def extractor() -> SwitchableExtractor:
    return SwitchableExtractor()


@pytest.fixture(autouse=True)
def reset_extractor(extractor: SwitchableExtractor):
    extractor.delegate = FakeExtractor(GOOD_EXTRACTION)
    yield


@pytest.fixture(scope="module")
def base_url(extractor: SwitchableExtractor):
    """Run uvicorn in a background thread on a free loopback port."""
    app.dependency_overrides[get_extractor] = lambda: extractor
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not server.started:
        if time.time() > deadline:
            pytest.fail("uvicorn did not start within 15s")
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)
    app.dependency_overrides.clear()


@pytest.fixture(scope="module")
def page(base_url):
    try:
        manager = playwright_api.sync_playwright().start()
    except Exception as exc:  # pragma: no cover - environment-dependent
        pytest.skip(f"playwright could not start: {exc}")
    try:
        browser = manager.chromium.launch()
    except Exception as exc:  # pragma: no cover - environment-dependent
        manager.stop()
        pytest.skip(f"headless Chromium is unavailable: {exc}")
    page = browser.new_page()
    yield page
    browser.close()
    manager.stop()


def submit_label(page, base_url: str, form: dict[str, str], is_import: bool = False) -> None:
    """Load the page, fill the form, attach the sample label, and click Check."""
    page.goto(base_url + "/")
    for field_id, value in form.items():
        page.fill(f"#{field_id}", value)
    if is_import:
        page.check("#is_import")
    page.set_input_files("#file-input", str(SAMPLE_LABEL))
    page.click("#check-button")


class TestHappyPath:
    def test_full_flow_renders_verdicts_and_timing(self, page, base_url):
        submit_label(
            page,
            base_url,
            {
                "brand": "Stone's Throw",
                "class_type": "Kentucky Straight Bourbon Whiskey",
                "abv": "45%",
                "net_contents": "75 cL",
                "producer": "Blue Ridge Distilling Co., 12 Main Street, Asheville, NC 28801",
            },
        )
        banner = page.locator("#banner-text")
        playwright_api.expect(banner).to_have_text("Everything matches", timeout=10_000)
        # All 7 fields render as table rows with icon + text verdicts.
        rows = page.locator("#results-body tr")
        playwright_api.expect(rows).to_have_count(7)
        playwright_api.expect(page.locator("#results-body")).to_contain_text("Brand name")
        playwright_api.expect(page.locator("#results-body")).to_contain_text("✅ Matches")
        # R2: elapsed time is visible on every result ("less than a second"
        # with the instant fake; "X.X seconds" with the real extractor).
        playwright_api.expect(page.locator("#timing")).to_contain_text("Checked in")
        playwright_api.expect(page.locator("#timing")).to_contain_text("second")
        # The uploaded label is previewed next to the results.
        playwright_api.expect(page.locator("#result-image")).to_be_visible()

    def test_optional_fields_left_blank_render_not_checked(self, page, base_url):
        submit_label(page, base_url, {"brand": "Stone's Throw"})
        playwright_api.expect(page.locator("#banner-text")).to_have_text(
            "Everything matches", timeout=10_000
        )
        playwright_api.expect(page.locator("#results-body")).to_contain_text("— Not checked")


class TestMismatchRendering:
    def test_warning_clause_diff_renders_readably(self, page, base_url, extractor):
        bad_warning = CANONICAL_WARNING.replace("may cause health problems", "might cause health problems")
        extractor.delegate = FakeExtractor(replace(GOOD_EXTRACTION, government_warning=bad_warning))
        submit_label(page, base_url, {"brand": "Stone's Throw"})
        playwright_api.expect(page.locator("#banner-text")).to_have_text(
            "Problems found", timeout=10_000
        )
        playwright_api.expect(page.locator("#results-body")).to_contain_text("❌ Doesn't match")
        # The clause diff is presented as prose, not raw JSON.
        diff = page.locator(".clause-diff")
        playwright_api.expect(diff).to_be_visible()
        playwright_api.expect(diff).to_contain_text("Part (2)")
        playwright_api.expect(diff).to_contain_text("Should say:")
        playwright_api.expect(diff).to_contain_text("Label says:")
        playwright_api.expect(page.locator("#results")).not_to_contain_text("clause_diff")


class TestErrorPaths:
    def test_extraction_failure_shows_friendly_callout_and_page_recovers(
        self, page, base_url, extractor
    ):
        extractor.delegate = FakeExtractor(error=ExtractionError("The label reading service is unavailable right now."))
        submit_label(page, base_url, {"brand": "Stone's Throw"})
        callout = page.locator("#error-callout")
        playwright_api.expect(callout).to_be_visible(timeout=10_000)
        playwright_api.expect(callout).to_contain_text("unavailable")
        playwright_api.expect(page.locator("#results")).to_be_hidden()
        # The page stays usable: the button re-enables and a retry succeeds.
        playwright_api.expect(page.locator("#check-button")).to_be_enabled()
        extractor.delegate = FakeExtractor(GOOD_EXTRACTION)
        page.click("#check-button")
        playwright_api.expect(page.locator("#banner-text")).to_have_text(
            "Everything matches", timeout=10_000
        )
        playwright_api.expect(callout).to_be_hidden()

    def test_missing_photo_prompts_instead_of_submitting(self, page, base_url):
        page.goto(base_url + "/")
        page.fill("#brand", "Stone's Throw")
        page.click("#check-button")
        callout = page.locator("#error-callout")
        playwright_api.expect(callout).to_be_visible()
        playwright_api.expect(callout).to_contain_text("add a photo")

    def test_non_image_file_shows_servers_friendly_message(self, page, base_url, tmp_path):
        not_an_image = tmp_path / "notes.txt"
        not_an_image.write_text("this is not an image")
        page.goto(base_url + "/")
        page.fill("#brand", "Stone's Throw")
        page.set_input_files("#file-input", str(not_an_image))
        page.click("#check-button")
        callout = page.locator("#error-callout")
        playwright_api.expect(callout).to_be_visible(timeout=10_000)
        playwright_api.expect(callout).to_contain_text("doesn't look like an image")
