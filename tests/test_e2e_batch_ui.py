"""L4 (mocked) — browser-driven E2E of the batch ("Check many labels") UI.

Same harness as test_e2e_ui.py: real FastAPI app on a loopback port with a
switchable fake extractor — no API key, no network beyond localhost.
Marked ``e2e``; skips cleanly when Playwright/Chromium is unavailable.
"""

from __future__ import annotations

import csv
import io
import socket
import threading
import time
from pathlib import Path

import pytest

playwright_api = pytest.importorskip(
    "playwright.sync_api", reason="playwright is not installed"
)

import uvicorn

from app.main import app, get_extractor
from app.models import ExtractedLabel
from app.rules.warning import CANONICAL_WARNING
from tests.conftest import HIGH_CONFIDENCE, FakeExtractor

pytestmark = pytest.mark.e2e

REPO_ROOT = Path(__file__).resolve().parent.parent
LABELS_DIR = REPO_ROOT / "eval" / "labels"

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

CSV_FIELDS = [
    "brand",
    "class_type",
    "abv",
    "net_contents",
    "producer",
    "origin_country",
    "government_warning",
]


class SlowFakeExtractor(FakeExtractor):
    """Adds per-label latency so chunked progress is observable."""

    def __init__(self, result: ExtractedLabel, delay: float) -> None:
        super().__init__(result)
        self.delay = delay

    def extract(self, image_bytes: bytes) -> ExtractedLabel:
        time.sleep(self.delay)
        return super().extract(image_bytes)


class SwitchableExtractor:
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


def label_paths(count: int) -> list[str]:
    paths = sorted(LABELS_DIR.glob("*.png"))[:count]
    assert len(paths) == count, f"expected {count} labels in eval/labels"
    return [str(p) for p in paths]


def open_batch_tab(page, base_url: str) -> None:
    page.goto(base_url + "/")
    page.click("#tab-batch")
    playwright_api.expect(page.locator("#batch-panel")).to_be_visible()
    playwright_api.expect(page.locator("#single-panel")).to_be_hidden()


def result_rows(page):
    return page.locator("#batch-results-body > tr:not(.detail-row)")


class TestBatchHappyPath:
    def test_five_labels_progress_table_and_csv_download(self, page, base_url, extractor, tmp_path):
        extractor.delegate = SlowFakeExtractor(GOOD_EXTRACTION, delay=0.15)
        open_batch_tab(page, base_url)
        page.set_input_files("#batch-file-input", label_paths(5))
        playwright_api.expect(page.locator("#batch-file-summary")).to_contain_text("5 photos selected")
        page.fill("#batch_brand", "Stone's Throw")
        page.fill("#batch_abv", "45%")
        page.click("#batch-check-button")

        # Progress is visible and counts against the real total while checking.
        playwright_api.expect(page.locator("#batch-progress")).to_be_visible()
        playwright_api.expect(page.locator("#batch-progress-text")).to_have_text(
            "Checked 0 of 5…"
        )

        # Final state: banner summary + one row per label, all matching.
        playwright_api.expect(page.locator("#batch-banner-text")).to_have_text(
            "5 labels checked — 5 match", timeout=15_000
        )
        playwright_api.expect(result_rows(page)).to_have_count(5)
        playwright_api.expect(page.locator("#batch-results-body")).to_contain_text("✅ Matches")
        playwright_api.expect(page.locator("#batch-results-body")).to_contain_text(
            "01-bourbon-clean.png"
        )
        playwright_api.expect(page.locator("#batch-progress")).to_be_hidden()

        # Expandable per-field detail.
        page.locator(".detail-toggle").first.click()
        detail = page.locator(".detail-table").first
        playwright_api.expect(detail).to_be_visible()
        playwright_api.expect(detail).to_contain_text("Government health warning")

        # CSV downloads and parses with the expected columns.
        with page.expect_download() as download_info:
            page.click("#batch-download")
        path = tmp_path / "results.csv"
        download_info.value.save_as(path)
        text = path.read_bytes().decode("utf-8-sig")
        rows = list(csv.reader(io.StringIO(text)))
        header = rows[0]
        assert header[:2] == ["filename", "overall_status"]
        for field in CSV_FIELDS:
            assert f"{field}_verdict" in header
            assert f"{field}_reason" in header
        assert header[-1] == "error"
        assert len(rows) == 6  # header + 5 labels
        assert rows[1][0] == "01-bourbon-clean.png"
        assert rows[1][1] == "match"
        verdict_at = header.index("brand_verdict")
        assert rows[1][verdict_at] == "match"


class TestChunkedProgress:
    def test_twelve_labels_progress_advances_between_chunks(self, page, base_url, extractor):
        """12 files = 2 sub-batches of 10 and 2: the counter must show the
        intermediate 'Checked 10 of 12…' state before finishing."""
        extractor.delegate = SlowFakeExtractor(GOOD_EXTRACTION, delay=0.25)
        open_batch_tab(page, base_url)
        page.set_input_files("#batch-file-input", label_paths(12))
        page.fill("#batch_brand", "Stone's Throw")
        # Polling-based expect() can miss the short-lived intermediate state;
        # record every progress-text mutation instead.
        page.evaluate(
            """() => {
                window.__progressStates = [];
                const node = document.querySelector('#batch-progress-text');
                new MutationObserver(() => window.__progressStates.push(node.textContent))
                    .observe(node, {childList: true, characterData: true, subtree: true});
            }"""
        )
        page.click("#batch-check-button")

        playwright_api.expect(page.locator("#batch-banner-text")).to_have_text(
            "12 labels checked — 12 match", timeout=15_000
        )
        playwright_api.expect(result_rows(page)).to_have_count(12)
        states = page.evaluate("() => window.__progressStates")
        assert "Checked 10 of 12…" in states, f"intermediate chunk state never shown: {states}"


class TestBatchErrorHandling:
    def test_corrupt_file_gets_error_row_others_succeed(self, page, base_url, tmp_path):
        not_an_image = tmp_path / "notes.txt"
        not_an_image.write_text("this is not an image")
        open_batch_tab(page, base_url)
        page.set_input_files("#batch-file-input", [*label_paths(2), str(not_an_image)])
        page.fill("#batch_brand", "Stone's Throw")
        page.click("#batch-check-button")

        playwright_api.expect(page.locator("#batch-banner-text")).to_have_text(
            "3 labels checked — 2 match, 1 couldn't be checked", timeout=15_000
        )
        playwright_api.expect(result_rows(page)).to_have_count(3)
        playwright_api.expect(page.locator("#batch-results-body")).to_contain_text(
            "⚠️ Couldn't check"
        )
        playwright_api.expect(page.locator("#batch-results-body")).to_contain_text(
            "doesn't look like an image"
        )

    def test_no_photos_prompts_instead_of_submitting(self, page, base_url):
        open_batch_tab(page, base_url)
        page.fill("#batch_brand", "Stone's Throw")
        page.click("#batch-check-button")
        callout = page.locator("#batch-error")
        playwright_api.expect(callout).to_be_visible()
        playwright_api.expect(callout).to_contain_text("add the label photos")

    def test_no_brand_and_no_csv_prompts(self, page, base_url):
        open_batch_tab(page, base_url)
        page.set_input_files("#batch-file-input", label_paths(1))
        page.click("#batch-check-button")
        callout = page.locator("#batch-error")
        playwright_api.expect(callout).to_be_visible()
        playwright_api.expect(callout).to_contain_text("brand name")


class TestManifestModeUI:
    def test_csv_manifest_drives_per_file_applications(self, page, base_url, tmp_path):
        manifest = tmp_path / "manifest.csv"
        manifest.write_text(
            "filename,brand,class_type,abv,net_contents,producer,origin_country,is_import\n"
            '01-bourbon-clean.png,"Stone\'s Throw",,45%,,,,false\n'
            "02-wine-clean.png,Completely Different Brand,,,,,,false\n",
            encoding="utf-8",
        )
        open_batch_tab(page, base_url)
        page.set_input_files("#batch-file-input", label_paths(2))
        page.set_input_files("#manifest-input", str(manifest))
        # Shared fields grey out when a manifest is chosen.
        playwright_api.expect(page.locator("#batch_brand")).to_be_disabled()
        page.click("#batch-check-button")

        playwright_api.expect(page.locator("#batch-banner-text")).to_have_text(
            "2 labels checked — 1 match, 1 don't match", timeout=15_000
        )
        playwright_api.expect(page.locator("#batch-results-body")).to_contain_text("❌ Doesn't match")
        playwright_api.expect(page.locator("#batch-results-body")).to_contain_text("Brand name")
