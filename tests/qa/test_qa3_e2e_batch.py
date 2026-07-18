"""QA gate 3 — adversarial browser E2E of the batch ("Check many labels") UI.

batch.js is NEW code that the Gate-2 XSS suite did not cover. Same discipline:
extraction output AND uploaded file names are UNTRUSTED; prove by attack that
every batch-rendered surface (results table, expandable detail, summary
reason, error rows) stays inert. Then attack the client-side CSV export with
formula-injection filenames and hostile error text, download through the real
browser, and re-parse the artifact with Python's csv module.

Harness identical to test_qa2_e2e.py: real FastAPI app on a loopback port,
switchable fake extractor, headless Chromium. Marked ``e2e``; skips cleanly
when Playwright/Chromium is unavailable. No API key, no network.
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
from PIL import Image

from app.extraction import ExtractionError
from app.main import app, get_extractor
from app.models import ExtractedLabel
from app.rules.warning import CANONICAL_WARNING
from tests.conftest import HIGH_CONFIDENCE, FakeExtractor

pytestmark = pytest.mark.e2e

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
LABELS_DIR = REPO_ROOT / "eval" / "labels"

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

# Hostile markup in every string field; the warning carries the script payload
# inside clause (2) so the clause-diff reason receives it in `found`.
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
    """In-memory upload payloads — lets file NAMES carry characters Windows
    forbids on disk (<, >, ")."""
    return [{"name": name, "mimeType": "image/png", "buffer": PNG} for name in names]


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
    pg = browser.new_page()
    yield pg
    browser.close()
    manager.stop()


def open_batch_tab(page, base_url: str) -> None:
    page.goto(base_url + "/")
    page.click("#tab-batch")
    playwright_api.expect(page.locator("#batch-panel")).to_be_visible()


def wait_for_banner(page):
    playwright_api.expect(page.locator("#batch-banner-text")).to_contain_text(
        "labels checked", timeout=15_000
    )


def download_results_csv(page, tmp_path) -> bytes:
    with page.expect_download() as download_info:
        page.click("#batch-download")
    path = tmp_path / "qa3-results.csv"
    download_info.value.save_as(path)
    return path.read_bytes()


class TestQa3BatchXSS:
    def test_qa3_hostile_values_and_filename_render_inert_in_batch_table(
        self, page, base_url, extractor
    ):
        extractor.delegate = FakeExtractor(HOSTILE_EXTRACTION)
        open_batch_tab(page, base_url)
        page.set_input_files(
            "#batch-file-input", files=memory_files([HOSTILE_FILENAME, "normal.png"])
        )
        page.fill("#batch_brand", "Legit Brand")
        page.fill("#batch_class_type", "Bourbon")
        page.fill("#batch_abv", "45%")
        page.fill("#batch_net_contents", "750 mL")
        page.fill("#batch_producer", "Distiller Co.")
        page.check("#batch_is_import")
        page.fill("#batch_origin_country", "France")
        page.click("#batch-check-button")
        wait_for_banner(page)

        # 1) Nothing executed.
        assert page.evaluate("window.__pwned") in (None, False), "XSS EXECUTED — pwned"

        # 2) The hostile FILENAME shows as literal text and no element materialized.
        body = page.locator("#batch-results-body")
        playwright_api.expect(body).to_contain_text("<img src=x onerror=")
        inner = page.eval_on_selector("#batch-results-body", "el => el.innerHTML")
        assert "<img src=x" not in inner, "raw <img> markup injected into the batch table"
        assert "&lt;img" in inner, "hostile text was not HTML-escaped in the DOM"
        injected = page.evaluate(
            "document.querySelectorAll('#batch-results img[src=\"x\"], #batch-results script').length"
        )
        assert injected == 0, "attacker <img>/<script> element was created"

        # 3) Expand every detail table (hostile VALUES + clause-diff reason) — still inert.
        toggles = page.locator("#batch-results-body .detail-toggle")
        for i in range(toggles.count()):
            toggles.nth(i).click()
        detail_html = page.eval_on_selector("#batch-results", "el => el.innerHTML")
        assert "<script>window.__pwned" not in detail_html, "raw <script> in detail table"
        assert page.evaluate("window.__pwned") in (None, False), "XSS on detail expansion"
        playwright_api.expect(
            page.locator("#batch-results-body .detail-table").first
        ).to_be_visible()

    def test_qa3_manifest_row_missing_error_reason_carries_hostile_filename_inert(
        self, page, base_url
    ):
        """A file with no manifest row gets a per-label error whose message
        embeds the (hostile) filename — that reason cell must stay inert."""
        open_batch_tab(page, base_url)
        page.set_input_files("#batch-file-input", files=memory_files([HOSTILE_FILENAME]))
        manifest_csv = "filename,brand\nsomething-else.png,Some Brand\n"
        page.set_input_files(
            "#manifest-input",
            files=[{"name": "manifest.csv", "mimeType": "text/csv",
                    "buffer": manifest_csv.encode("utf-8")}],
        )
        page.click("#batch-check-button")
        wait_for_banner(page)

        assert page.evaluate("window.__pwned") in (None, False)
        body = page.locator("#batch-results-body")
        playwright_api.expect(body).to_contain_text("Couldn't check")
        playwright_api.expect(body).to_contain_text("doesn't have a row for")
        inner = page.eval_on_selector("#batch-results-body", "el => el.innerHTML")
        assert "<img src=x" not in inner
        assert page.evaluate(
            "document.querySelectorAll('#batch-results img[src=\"x\"]').length"
        ) == 0

    def test_qa3_structural_manifest_error_with_payload_renders_inert(
        self, page, base_url
    ):
        """A bad is_import value is echoed back verbatim in the 400 message;
        the chunk-failure path paints it into every row's reason cell. Inert?"""
        open_batch_tab(page, base_url)
        page.set_input_files("#batch-file-input", files=memory_files(["a.png"]))
        manifest_csv = f"filename,brand,is_import\na.png,Brand,oui{XSS_IMG}\n"
        page.set_input_files(
            "#manifest-input",
            files=[{"name": "manifest.csv", "mimeType": "text/csv",
                    "buffer": manifest_csv.encode("utf-8")}],
        )
        page.click("#batch-check-button")
        wait_for_banner(page)

        assert page.evaluate("window.__pwned") in (None, False)
        body = page.locator("#batch-results-body")
        playwright_api.expect(body).to_contain_text("is_import")
        inner = page.eval_on_selector("#batch-results-body", "el => el.innerHTML")
        assert "<img src=x" not in inner
        assert page.evaluate(
            "document.querySelectorAll('#batch-results img[src=\"x\"]').length"
        ) == 0


class TestQa3CsvExportAttacks:
    EXPECTED_COLUMNS = 2 + 7 * 2 + 1  # filename, overall_status, 7×(verdict, reason), error

    def test_qa3_formula_injection_filenames_are_guarded_and_structure_survives(
        self, page, base_url, tmp_path
    ):
        hostile_names = [
            "=HYPERLINK(A1).png",
            "+cmd-launch.png",
            "-2+3.png",
            "@SUM(A1).png",
            "comma,semicolon;.png",
            "wine🍷émoji.png",
        ]
        open_batch_tab(page, base_url)
        page.set_input_files("#batch-file-input", files=memory_files(hostile_names))
        page.fill("#batch_brand", "Stone's Throw")
        page.click("#batch-check-button")
        wait_for_banner(page)

        raw = download_results_csv(page, tmp_path)

        # Excel-opens-cleanly proxy: BOM, CRLF row endings.
        assert raw.startswith(b"\xef\xbb\xbf"), "CSV must start with a UTF-8 BOM"
        text = raw.decode("utf-8-sig")
        assert "\r\n" in text, "CSV must use CRLF line endings"

        rows = list(csv.reader(io.StringIO(text)))
        assert len(rows) == 1 + len(hostile_names)
        assert all(len(row) == self.EXPECTED_COLUMNS for row in rows), (
            "inconsistent field count: " + repr([len(r) for r in rows])
        )

        filenames = [row[0] for row in rows[1:]]
        # Formula-leading filenames must come back neutralized with a leading '.
        for name in ("=HYPERLINK(A1).png", "+cmd-launch.png", "-2+3.png", "@SUM(A1).png"):
            assert ("'" + name) in filenames, f"{name!r} not formula-guarded: {filenames}"
        for cell in (row[0] for row in rows[1:]):
            assert not cell.startswith(("=", "+", "-", "@")), f"unguarded cell {cell!r}"
        # Comma/semicolon and non-ASCII names survive quoting intact.
        assert "comma,semicolon;.png" in filenames
        assert "wine🍷émoji.png" in filenames

    def test_qa3_error_message_with_newline_and_quotes_survives_export(
        self, page, base_url, extractor, tmp_path
    ):
        extractor.delegate = FakeExtractor(
            error=ExtractionError('Reader down\nline two, "quoted" and =SUM(A1)')
        )
        open_batch_tab(page, base_url)
        page.set_input_files("#batch-file-input", files=memory_files(["a.png", "b.png"]))
        page.fill("#batch_brand", "Stone's Throw")
        page.click("#batch-check-button")
        wait_for_banner(page)

        raw = download_results_csv(page, tmp_path)
        rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig"))))
        assert len(rows) == 3  # header + 2 labels — embedded newline did NOT split rows
        assert all(len(row) == self.EXPECTED_COLUMNS for row in rows)
        header = rows[0]
        error_at = header.index("error")
        for row in rows[1:]:
            assert row[1] == "error"
            cell = row[error_at]
            assert "Reader down\nline two" in cell, "newline inside the cell was lost"
            assert '"quoted"' in cell, "double quotes were mangled"
            assert "=SUM(A1)" in cell  # mid-cell = is fine; only leading = is a formula


class TestQa3BatchUxRegression:
    def test_qa3_focus_lands_on_batch_banner_on_completion(self, page, base_url):
        open_batch_tab(page, base_url)
        page.set_input_files("#batch-file-input", files=memory_files(["a.png"]))
        page.fill("#batch_brand", "Stone's Throw")
        page.click("#batch-check-button")
        wait_for_banner(page)
        assert page.evaluate("document.activeElement && document.activeElement.id") == (
            "batch-banner"
        ), "focus must move to the summary banner so screen readers announce completion"

    def test_qa3_tab_switch_after_results_preserves_batch_state(self, page, base_url):
        open_batch_tab(page, base_url)
        page.set_input_files("#batch-file-input", files=memory_files(["a.png"]))
        page.fill("#batch_brand", "Stone's Throw")
        page.click("#batch-check-button")
        wait_for_banner(page)
        page.click("#tab-single")
        playwright_api.expect(page.locator("#batch-panel")).to_be_hidden()
        page.click("#tab-batch")
        playwright_api.expect(page.locator("#batch-results")).to_be_visible()
        playwright_api.expect(page.locator("#batch-banner-text")).to_contain_text(
            "labels checked"
        )

    def test_qa3_selecting_new_photos_after_results_clears_stale_batch_results(
        self, page, base_url
    ):
        """Gate-2 finding #2 (fixed for the single flow): a verdict panel from
        the PREVIOUS run must not sit on screen next to newly selected photos.
        Same bar for the batch flow: after picking new files, the old results
        table (old filenames, old verdicts) must not remain visible."""
        open_batch_tab(page, base_url)
        page.set_input_files("#batch-file-input", files=memory_files(["first-run.png"]))
        page.fill("#batch_brand", "Stone's Throw")
        page.click("#batch-check-button")
        wait_for_banner(page)

        # Pick DIFFERENT files without clicking Check.
        page.set_input_files("#batch-file-input", files=memory_files(["second-run.png"]))
        assert page.locator("#batch-results").is_visible() is False, (
            "stale batch results (for first-run.png) remain visible after "
            "selecting new photos — same class of bug as Gate-2 finding #2"
        )
