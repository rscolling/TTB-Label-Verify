"""L4 (mocked) — browser-driven E2E of the unified worksheet UI (WP5).

Boots the real FastAPI app on a loopback port inside this process, with the
extractor dependency overridden to a switchable fake — no API key, no network
beyond localhost. Drives headless Chromium via Playwright.

This file covers the single-photo / no-CSV side of the worksheet flow (the
replacement for the old single-label tab), the review drill-down panel,
keyboard access, error paths, and XSS-inertness of the worksheet + panel.
The multi-file + submittal-CSV flow lives in test_e2e_batch_ui.py.

Marked ``e2e``; skips cleanly when Playwright/Chromium is unavailable.
"""

from __future__ import annotations

import io
import re
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
from PIL import Image

from app.extraction import ExtractionError
from app.main import app, get_extractor
from app.models import ExtractedLabel
from app.rules.warning import CANONICAL_WARNING
from tests.conftest import HIGH_CONFIDENCE, FakeExtractor

pytestmark = pytest.mark.e2e

REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_LABEL = REPO_ROOT / "eval" / "labels" / "01-bourbon-clean.png"

STAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
ELAPSED_RE = re.compile(r"^\d+\.\ds$")  # per-label elapsed time, e.g. "4.9s"

XSS_IMG = '<img src=x onerror="window.__pwned=1">'
XSS_SCRIPT = "<script>window.__pwned=1</script>"
HOSTILE_FILENAME = "<img src=x onerror=window.__pwned=1>.png"

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

HOSTILE_EXTRACTION = ExtractedLabel(
    brand=XSS_IMG,
    class_type=f"Bourbon {XSS_IMG}",
    alcohol_content=f"45% {XSS_IMG}",
    net_contents=f"750 mL {XSS_IMG}",
    producer=f"Distiller {XSS_SCRIPT}",
    origin_country=f"France {XSS_IMG}",
    government_warning=CANONICAL_WARNING.replace(
        "may cause health problems", f"may cause {XSS_SCRIPT}"
    ),
    warning_prefix_appears_bold=True,
    confidence=dict(HIGH_CONFIDENCE),
    label_detected=True,
)


def tiny_png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (2, 2), "white").save(buffer, format="PNG")
    return buffer.getvalue()


PNG = tiny_png()


def memory_files(names: list[str]) -> list[dict]:
    """In-memory upload payloads — file NAMES may carry characters Windows
    forbids on disk (<, >, ")."""
    return [{"name": name, "mimeType": "image/png", "buffer": PNG} for name in names]


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


def scan_files(page, base_url: str, paths_or_payloads) -> None:
    """Load the page, attach the photos (no CSV), and click Scan Labels."""
    page.goto(base_url + "/")
    if paths_or_payloads and isinstance(paths_or_payloads[0], dict):
        page.set_input_files("#file-input", files=paths_or_payloads)
    else:
        page.set_input_files("#file-input", paths_or_payloads)
    page.click("#scan-button")


def wait_for_banner(page):
    playwright_api.expect(page.locator("#banner-text")).to_contain_text(
        "scanned", timeout=15_000
    )


def worksheet_rows(page):
    return page.locator("#worksheet-body > tr.worksheet-row")


class TestSinglephotoNoCsv:
    """One image, no submittal CSV — this replaces the old single-label flow."""

    def test_scan_fills_extracted_columns_and_flags_the_row(self, page, base_url):
        scan_files(page, base_url, [str(SAMPLE_LABEL)])
        wait_for_banner(page)
        playwright_api.expect(page.locator("#banner-text")).to_have_text(
            "1 label scanned — 0 passed, 1 needs review"
        )

        row = worksheet_rows(page).first
        playwright_api.expect(worksheet_rows(page)).to_have_count(1)
        # Serial in scan order, zero-padded, with the review flag.
        playwright_api.expect(row.locator("td").first).to_contain_text("001")
        playwright_api.expect(row.locator(".flag")).to_have_count(1)
        # Extracted values fill the field columns.
        playwright_api.expect(row.locator('td[data-label="Brand name"]')).to_contain_text(
            "STONE'S THROW"
        )
        playwright_api.expect(
            row.locator('td[data-label="Alcohol content"]')
        ).to_contain_text("45% Alc./Vol.")
        # Submittal-checked cells carry the no-data review mark — NOT a match.
        brand_mark = row.locator('td[data-label="Brand name"] .mark')
        playwright_api.expect(brand_mark).to_have_attribute(
            "title", "No submittal data — needs review"
        )
        # The statutory health-warning check still runs and can genuinely match.
        playwright_api.expect(
            row.locator('td[data-label="Health warning"] .mark')
        ).to_have_attribute("title", "Matches")
        # Score column: never a silent pass without submittal data.
        playwright_api.expect(row.locator('td[data-label="Score"]')).to_have_text(
            "No submittal data — needs review"
        )
        playwright_api.expect(row.locator(".status-badge")).to_have_text("REVIEW")
        # Flagged rows are tinted.
        assert "row-review" in (row.get_attribute("class") or "")

    def test_row_has_a_scan_timestamp(self, page, base_url):
        scan_files(page, base_url, [str(SAMPLE_LABEL)])
        wait_for_banner(page)
        stamp = worksheet_rows(page).first.locator('td[data-label="Scanned at"]').text_content()
        assert stamp is not None and STAMP_RE.match(stamp.strip()), (
            f"timestamp cell {stamp!r} does not match YYYY-MM-DD HH:MM:SS"
        )

    def test_row_shows_per_label_elapsed_time(self, page, base_url):
        # R2: "results back in about 5 seconds" — the elapsed time must be
        # displayed in the UI on every result (audit drift fix: the WP5
        # worksheet dropped it; restored as a per-row Time column).
        scan_files(page, base_url, [str(SAMPLE_LABEL)])
        wait_for_banner(page)
        cell = worksheet_rows(page).first.locator('td[data-label="Time"]').text_content()
        cell = (cell or "").strip()
        assert ELAPSED_RE.match(cell), f"Time cell {cell!r} does not look like 'N.Ns'"
        assert 0.0 <= float(cell[:-1]) < 60.0, cell

    def test_focus_lands_on_banner_when_scan_completes(self, page, base_url):
        scan_files(page, base_url, [str(SAMPLE_LABEL)])
        wait_for_banner(page)
        assert page.evaluate("document.activeElement && document.activeElement.id") == "banner"

    def test_warning_mismatch_without_csv_still_fails_the_row(
        self, page, base_url, extractor
    ):
        # No submittal data caps a row at REVIEW — but a statutory warning
        # violation is a real failure and must show as FAIL, not hide behind
        # the no-data state.
        bad = CANONICAL_WARNING.replace("may cause health problems", "might cause health problems")
        extractor.delegate = FakeExtractor(replace(GOOD_EXTRACTION, government_warning=bad))
        scan_files(page, base_url, [str(SAMPLE_LABEL)])
        wait_for_banner(page)
        row = worksheet_rows(page).first
        playwright_api.expect(row.locator(".status-badge")).to_have_text("FAIL")
        assert "row-fail" in (row.get_attribute("class") or "")


class TestReviewDrillDown:
    def test_review_button_opens_panel_with_image_and_comparison(
        self, page, base_url, extractor
    ):
        bad = CANONICAL_WARNING.replace("may cause health problems", "might cause health problems")
        extractor.delegate = FakeExtractor(replace(GOOD_EXTRACTION, government_warning=bad))
        scan_files(page, base_url, [str(SAMPLE_LABEL)])
        wait_for_banner(page)

        page.locator(".review-button").first.click()
        panel = page.locator(".detail-panel")
        playwright_api.expect(panel).to_be_visible()
        # Focus moves into the panel when it opens.
        assert page.evaluate(
            "document.activeElement && document.activeElement.className"
        ).startswith("detail-panel")
        # The label image, large, from a client-side object URL (R8: no server storage).
        src = panel.locator(".detail-figure img").get_attribute("src")
        assert src and src.startswith("blob:"), f"detail image should be a blob: URL, got {src!r}"
        # Scan timestamp + per-label elapsed time in the panel header
        # (addendum + R2 audit drift fix): "Scanned 2026-07-18 19:42:07 · 4.9s".
        stamp_text = panel.locator(".detail-stamp").text_content() or ""
        assert re.search(
            r"Scanned \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} · \d+\.\ds", stamp_text
        ), stamp_text
        # Field-by-field comparison: submittal vs scan, verdict, reason.
        detail = panel.locator(".detail-table")
        playwright_api.expect(detail).to_contain_text("Submittal form says")
        playwright_api.expect(detail).to_contain_text("Scan found")
        playwright_api.expect(detail).to_contain_text("Health warning")
        playwright_api.expect(detail).to_contain_text("No submittal data — needs review")
        # The warning clause diff renders as prose inside the panel.
        diff = panel.locator(".clause-diff")
        playwright_api.expect(diff).to_be_visible()
        playwright_api.expect(diff).to_contain_text("Part (2)")
        playwright_api.expect(diff).to_contain_text("Should say:")
        playwright_api.expect(diff).to_contain_text("Label says:")
        playwright_api.expect(page.locator("#results")).not_to_contain_text("clause_diff")

    def test_escape_closes_panel_and_returns_focus_to_the_button(self, page, base_url):
        scan_files(page, base_url, [str(SAMPLE_LABEL)])
        wait_for_banner(page)
        button = page.locator(".review-button").first
        button.click()
        playwright_api.expect(page.locator(".detail-panel")).to_be_visible()
        page.keyboard.press("Escape")
        playwright_api.expect(page.locator(".detail-panel")).to_have_count(0)
        assert page.evaluate(
            "document.activeElement && document.activeElement.className"
        ).endswith("review-button")
        playwright_api.expect(button).to_have_attribute("aria-expanded", "false")

    def test_review_control_is_a_real_button_and_keyboard_operable(self, page, base_url):
        scan_files(page, base_url, [str(SAMPLE_LABEL)])
        wait_for_banner(page)
        assert page.evaluate(
            "document.querySelector('.review-button').tagName"
        ) == "BUTTON"
        page.locator(".review-button").first.focus()
        page.keyboard.press("Enter")
        playwright_api.expect(page.locator(".detail-panel")).to_be_visible()

    def test_clicking_the_row_itself_opens_the_panel(self, page, base_url):
        scan_files(page, base_url, [str(SAMPLE_LABEL)])
        wait_for_banner(page)
        worksheet_rows(page).first.locator('td[data-label="Brand name"]').click()
        playwright_api.expect(page.locator(".detail-panel")).to_be_visible()
        playwright_api.expect(
            page.locator(".review-button").first
        ).to_have_attribute("aria-expanded", "true")


class TestErrorPaths:
    def test_corrupt_file_gets_error_row_others_still_scan(self, page, base_url, tmp_path):
        not_an_image = tmp_path / "notes.txt"
        not_an_image.write_text("this is not an image")
        scan_files(page, base_url, [str(SAMPLE_LABEL), str(not_an_image)])
        wait_for_banner(page)
        playwright_api.expect(page.locator("#banner-text")).to_have_text(
            "2 labels scanned — 0 passed, 1 needs review, 1 couldn't be scanned"
        )
        playwright_api.expect(worksheet_rows(page)).to_have_count(2)
        error_row = worksheet_rows(page).nth(1)
        playwright_api.expect(error_row.locator(".status-badge")).to_have_text("ERROR")
        playwright_api.expect(error_row).to_contain_text("doesn't look like an image")
        # The error row still gets a serial, a timestamp, and a drill-down.
        playwright_api.expect(error_row.locator("td").first).to_contain_text("002")
        error_row.locator(".review-button").click()
        panel = page.locator(".detail-panel")
        playwright_api.expect(panel).to_contain_text("doesn't look like an image")
        playwright_api.expect(panel.locator(".detail-figure img")).to_be_visible()

    def test_no_photos_prompts_instead_of_submitting(self, page, base_url):
        page.goto(base_url + "/")
        page.click("#scan-button")
        callout = page.locator("#error-callout")
        playwright_api.expect(callout).to_be_visible()
        playwright_api.expect(callout).to_contain_text("add the label photos")

    def test_extraction_failure_becomes_error_rows_and_page_recovers(
        self, page, base_url, extractor
    ):
        extractor.delegate = FakeExtractor(
            error=ExtractionError("The label reading service is unavailable right now.")
        )
        scan_files(page, base_url, [str(SAMPLE_LABEL)])
        wait_for_banner(page)
        playwright_api.expect(worksheet_rows(page).first).to_contain_text("unavailable")
        playwright_api.expect(
            worksheet_rows(page).first.locator(".status-badge")
        ).to_have_text("ERROR")
        # The page stays usable: the button re-enables and a retry succeeds.
        playwright_api.expect(page.locator("#scan-button")).to_be_enabled()
        extractor.delegate = FakeExtractor(GOOD_EXTRACTION)
        page.click("#scan-button")
        playwright_api.expect(page.locator("#banner-text")).to_have_text(
            "1 label scanned — 0 passed, 1 needs review", timeout=15_000
        )

    def test_new_photo_selection_clears_stale_worksheet(self, page, base_url):
        scan_files(page, base_url, [str(SAMPLE_LABEL)])
        wait_for_banner(page)
        page.set_input_files(
            "#file-input", str(SAMPLE_LABEL.with_name("02-wine-clean.png"))
        )
        assert page.locator("#results").is_visible() is False, (
            "stale worksheet rows remain visible after selecting new photos"
        )


class TestXSSInertRendering:
    """Extracted values, file names, and reasons are hostile until proven
    otherwise. Mirror of the tests/qa discipline, against the worksheet UI."""

    def test_hostile_extraction_and_filename_render_inert_in_worksheet_and_panel(
        self, page, base_url, extractor
    ):
        extractor.delegate = FakeExtractor(HOSTILE_EXTRACTION)
        scan_files(page, base_url, memory_files([HOSTILE_FILENAME]))
        wait_for_banner(page)

        # 1) Nothing executed.
        assert page.evaluate("window.__pwned") in (None, False), "XSS EXECUTED — pwned"

        # 2) Hostile extracted values and the hostile FILENAME appear as
        #    literal text, escaped in the DOM; no attacker element materialized.
        body = page.locator("#worksheet-body")
        playwright_api.expect(body).to_contain_text("<img src=x onerror=")
        inner = page.eval_on_selector("#worksheet-body", "el => el.innerHTML")
        assert "<img src=x" not in inner, "raw <img> markup injected into the worksheet"
        assert "&lt;img" in inner, "hostile text was not HTML-escaped in the DOM"
        injected = page.evaluate(
            "document.querySelectorAll('#results img[src=\"x\"], #results script').length"
        )
        assert injected == 0, "attacker <img>/<script> element was created"

        # 3) Open the drill-down: hostile values, reasons, and the clause-diff
        #    `found` text all land in the panel — still inert.
        page.locator(".review-button").first.click()
        playwright_api.expect(page.locator(".detail-panel")).to_be_visible()
        panel_html = page.eval_on_selector("#results", "el => el.innerHTML")
        assert "<script>window.__pwned" not in panel_html, "raw <script> in detail panel"
        assert page.evaluate("window.__pwned") in (None, False), "XSS on panel open"

    def test_hostile_submittal_csv_values_render_inert_in_panel(self, page, base_url):
        # The "Submittal form says" column renders CSV-supplied values — also
        # untrusted (a reviewer opens spreadsheets they didn't write).
        page.goto(base_url + "/")
        page.set_input_files("#file-input", files=memory_files(["a.png"]))
        manifest = f'filename,brand\na.png,"{XSS_IMG}"\n'
        page.set_input_files(
            "#csv-input",
            files=[{"name": "submittal.csv", "mimeType": "text/csv",
                    "buffer": manifest.encode("utf-8")}],
        )
        page.click("#scan-button")
        wait_for_banner(page)
        page.locator(".review-button").first.click()
        playwright_api.expect(page.locator(".detail-panel")).to_be_visible()
        assert page.evaluate("window.__pwned") in (None, False)
        inner = page.eval_on_selector(".detail-panel", "el => el.innerHTML")
        assert "<img src=x" not in inner, "raw <img> from CSV value in the panel"
        assert page.evaluate(
            "document.querySelectorAll('#results img[src=\"x\"]').length"
        ) == 0

    def test_hostile_error_message_renders_inert_in_worksheet(
        self, page, base_url, extractor
    ):
        extractor.delegate = FakeExtractor(error=ExtractionError(f"Reader down {XSS_IMG}"))
        scan_files(page, base_url, memory_files(["a.png"]))
        wait_for_banner(page)
        assert page.evaluate("window.__pwned") in (None, False)
        inner = page.eval_on_selector("#worksheet-body", "el => el.innerHTML")
        assert "<img src=x" not in inner, "raw markup in the error row"
        playwright_api.expect(page.locator("#worksheet-body")).to_contain_text("<img")
