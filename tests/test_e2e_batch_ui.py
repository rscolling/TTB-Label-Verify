"""L4 (mocked) — browser-driven E2E of the worksheet flow with many photos
and the submittal-form CSV (WP5).

Same harness as test_e2e_ui.py: real FastAPI app on a loopback port with a
switchable fake extractor — no API key, no network beyond localhost.
Covers: happy multi-photo scan against a submittal CSV, per-row pass/fail
scoring, chunked progress, the photo-missing-from-CSV error row, the no-CSV
multi-photo path, and the CSV export (downloaded through the real browser
and re-parsed with Python's csv module).

Marked ``e2e``; skips cleanly when Playwright/Chromium is unavailable.
"""

from __future__ import annotations

import csv
import io
import re
import socket
import threading
import time
from datetime import datetime
from pathlib import Path

import pytest

playwright_api = pytest.importorskip(
    "playwright.sync_api", reason="playwright is not installed"
)

import uvicorn
from PIL import Image

from app.main import app, get_extractor
from app.models import ExtractedLabel
from app.rules.warning import CANONICAL_WARNING
from tests.conftest import HIGH_CONFIDENCE, FakeExtractor

pytestmark = pytest.mark.e2e

REPO_ROOT = Path(__file__).resolve().parent.parent
LABELS_DIR = REPO_ROOT / "eval" / "labels"

STAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
ELAPSED_RE = re.compile(r"^\d+\.\ds$")       # per-row Time cell, e.g. "4.9s"
CSV_SECONDS_RE = re.compile(r"^\d+\.\d$")    # processing_seconds export column, e.g. "4.9"

CSV_FIELDS = [
    "brand",
    "class_type",
    "abv",
    "net_contents",
    "producer",
    "origin_country",
    "government_warning",
]

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

# One shared submittal row body that fully matches GOOD_EXTRACTION:
# 6 applicable fields (origin is N/A for domestic) -> "6/6 fields match".
MATCHING_ROW = '"Stone\'s Throw","Kentucky Straight Bourbon Whiskey",45%,750 mL,"Blue Ridge Distilling Co., 12 Main Street, Asheville, NC 28801",,false'


def tiny_png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (2, 2), "white").save(buffer, format="PNG")
    return buffer.getvalue()


PNG = tiny_png()


def memory_files(names: list[str]) -> list[dict]:
    return [{"name": name, "mimeType": "image/png", "buffer": PNG} for name in names]


def csv_payload(text: str, name: str = "submittal.csv") -> list[dict]:
    return [{"name": name, "mimeType": "text/csv", "buffer": text.encode("utf-8")}]


def matching_manifest(filenames: list[str]) -> str:
    header = "filename,brand,class_type,abv,net_contents,producer,origin_country,is_import\n"
    return header + "".join(f"{name},{MATCHING_ROW}\n" for name in filenames)


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


def worksheet_rows(page):
    return page.locator("#worksheet-body > tr.worksheet-row")


def wait_for_banner(page, timeout: int = 15_000):
    playwright_api.expect(page.locator("#banner-text")).to_contain_text(
        "scanned", timeout=timeout
    )


def download_worksheet_csv(page, tmp_path) -> bytes:
    with page.expect_download() as download_info:
        page.click("#download-csv")
    path = tmp_path / "worksheet.csv"
    download_info.value.save_as(path)
    return path.read_bytes()


class TestHappyScanWithCsv:
    def test_five_photos_all_pass_with_serials_and_scores(self, page, base_url, tmp_path):
        paths = label_paths(5)
        page.goto(base_url + "/")
        page.set_input_files("#file-input", paths)
        playwright_api.expect(page.locator("#file-summary")).to_contain_text("5 photos selected")
        page.set_input_files(
            "#csv-input", files=csv_payload(matching_manifest([Path(p).name for p in paths]))
        )
        playwright_api.expect(page.locator("#csv-status")).to_contain_text("submittal.csv")
        page.click("#scan-button")

        playwright_api.expect(page.locator("#banner-text")).to_have_text(
            "5 labels scanned — 5 passed", timeout=15_000
        )
        playwright_api.expect(worksheet_rows(page)).to_have_count(5)

        # Serials 001..005 in scan order.
        serials = [
            (worksheet_rows(page).nth(i).locator("td").first.text_content() or "").strip()
            for i in range(5)
        ]
        assert serials == ["001", "002", "003", "004", "005"], serials

        first = worksheet_rows(page).first
        playwright_api.expect(first).to_contain_text("01-bourbon-clean.png")
        # Every applicable field matched -> per-cell ✓ marks, 6/6 score, PASS.
        playwright_api.expect(
            first.locator('td[data-label="Brand name"] .mark')
        ).to_have_attribute("title", "Matches")
        playwright_api.expect(first.locator('td[data-label="Score"]')).to_have_text(
            "6/6 fields match"
        )
        playwright_api.expect(first.locator(".status-badge")).to_have_text("PASS")
        assert "row-fail" not in (first.get_attribute("class") or "")
        assert first.locator(".flag").count() == 0, "a passing row must not be flagged"
        # Thumbnail from a client-side object URL.
        thumb_src = first.locator(".thumb").get_attribute("src")
        assert thumb_src and thumb_src.startswith("blob:")
        # Timestamp column present and well-formed on every row.
        for i in range(5):
            stamp = (
                worksheet_rows(page).nth(i).locator('td[data-label="Scanned at"]').text_content()
                or ""
            ).strip()
            assert STAMP_RE.match(stamp), f"row {i} stamp {stamp!r}"
        # R2 audit drift fix: per-label elapsed time is back on every row —
        # a Time column showing that label's processing_time_ms as "N.Ns".
        for i in range(5):
            cell = (
                worksheet_rows(page).nth(i).locator('td[data-label="Time"]').text_content()
                or ""
            ).strip()
            assert ELAPSED_RE.match(cell), f"row {i} Time cell {cell!r}"
            assert 0.0 <= float(cell[:-1]) < 60.0, f"row {i} implausible elapsed {cell!r}"

    def test_mismatched_row_is_failed_and_flagged(self, page, base_url):
        paths = label_paths(2)
        names = [Path(p).name for p in paths]
        manifest = (
            "filename,brand,class_type,abv,net_contents,producer,origin_country,is_import\n"
            f"{names[0]},{MATCHING_ROW}\n"
            f"{names[1]},Completely Different Brand,,,,,,false\n"
        )
        page.goto(base_url + "/")
        page.set_input_files("#file-input", paths)
        page.set_input_files("#csv-input", files=csv_payload(manifest))
        page.click("#scan-button")

        playwright_api.expect(page.locator("#banner-text")).to_have_text(
            "2 labels scanned — 1 passed, 1 failed", timeout=15_000
        )
        bad = worksheet_rows(page).nth(1)
        playwright_api.expect(bad.locator(".status-badge")).to_have_text("FAIL")
        assert "row-fail" in (bad.get_attribute("class") or "")
        playwright_api.expect(bad.locator(".flag")).to_have_count(1)
        playwright_api.expect(
            bad.locator('td[data-label="Brand name"] .mark')
        ).to_have_attribute("title", "Doesn't match")
        # Score counts the applicable matches (brand missed; warning matched).
        playwright_api.expect(bad.locator('td[data-label="Score"]')).to_contain_text(
            "fields match"
        )

    def test_photo_missing_from_csv_becomes_error_row(self, page, base_url):
        paths = label_paths(2)
        names = [Path(p).name for p in paths]
        manifest = (
            "filename,brand,class_type,abv,net_contents,producer,origin_country,is_import\n"
            f"{names[0]},{MATCHING_ROW}\n"
        )
        page.goto(base_url + "/")
        page.set_input_files("#file-input", paths)
        page.set_input_files("#csv-input", files=csv_payload(manifest))
        page.click("#scan-button")

        playwright_api.expect(page.locator("#banner-text")).to_have_text(
            "2 labels scanned — 1 passed, 1 couldn't be scanned", timeout=15_000
        )
        error_row = worksheet_rows(page).nth(1)
        playwright_api.expect(error_row.locator(".status-badge")).to_have_text("ERROR")
        playwright_api.expect(error_row).to_contain_text("doesn't have a row for")
        playwright_api.expect(error_row.locator(".flag")).to_have_count(1)


class TestScanWithoutCsv:
    def test_multi_photo_scan_without_csv_flags_every_row(self, page, base_url):
        page.goto(base_url + "/")
        page.set_input_files("#file-input", label_paths(3))
        page.click("#scan-button")

        playwright_api.expect(page.locator("#banner-text")).to_have_text(
            "3 labels scanned — 0 passed, 3 need review", timeout=15_000
        )
        for i in range(3):
            row = worksheet_rows(page).nth(i)
            playwright_api.expect(row.locator(".status-badge")).to_have_text("REVIEW")
            playwright_api.expect(row.locator('td[data-label="Score"]')).to_have_text(
                "No submittal data — needs review"
            )
            playwright_api.expect(row.locator(".flag")).to_have_count(1)
            # Extracted data still fills the columns.
            playwright_api.expect(row.locator('td[data-label="Brand name"]')).to_contain_text(
                "STONE'S THROW"
            )


class TestChunkedProgress:
    def test_twelve_photos_progress_advances_between_chunks(self, page, base_url, extractor):
        """12 files = 2 sub-batches of 10 and 2: the counter must show the
        intermediate 'Scanned 10 of 12…' state before finishing, and rows
        must appear as chunks land."""
        extractor.delegate = SlowFakeExtractor(GOOD_EXTRACTION, delay=0.25)
        paths = label_paths(12)
        page.goto(base_url + "/")
        page.set_input_files("#file-input", paths)
        page.set_input_files(
            "#csv-input", files=csv_payload(matching_manifest([Path(p).name for p in paths]))
        )
        # Polling-based expect() can miss the short-lived intermediate state;
        # record every progress-text mutation instead.
        page.evaluate(
            """() => {
                window.__progressStates = [];
                const node = document.querySelector('#progress-text');
                new MutationObserver(() => window.__progressStates.push(node.textContent))
                    .observe(node, {childList: true, characterData: true, subtree: true});
            }"""
        )
        page.click("#scan-button")

        playwright_api.expect(page.locator("#banner-text")).to_have_text(
            "12 labels scanned — 12 passed", timeout=20_000
        )
        playwright_api.expect(worksheet_rows(page)).to_have_count(12)
        states = page.evaluate("() => window.__progressStates")
        assert "Scanned 10 of 12…" in states, f"intermediate chunk state never shown: {states}"
        playwright_api.expect(page.locator("#progress-block")).to_be_hidden()
        # Serials remained sequential across the chunk boundary.
        serial_11 = (worksheet_rows(page).nth(10).locator("td").first.text_content() or "").strip()
        assert serial_11 == "011", serial_11


class TestCsvExport:
    # serial,filename,scan_timestamp,processing_seconds,pass_fail,score, 7×(verdict,reason), error
    EXPECTED_COLUMNS = 6 + 7 * 2 + 1

    def test_export_reparses_with_serial_passfail_score_and_timestamp(
        self, page, base_url, tmp_path
    ):
        paths = label_paths(2)
        names = [Path(p).name for p in paths]
        manifest = (
            "filename,brand,class_type,abv,net_contents,producer,origin_country,is_import\n"
            f"{names[0]},{MATCHING_ROW}\n"
            f"{names[1]},Completely Different Brand,,,,,,false\n"
        )
        page.goto(base_url + "/")
        page.set_input_files("#file-input", paths)
        page.set_input_files("#csv-input", files=csv_payload(manifest))
        page.click("#scan-button")
        wait_for_banner(page)

        raw = download_worksheet_csv(page, tmp_path)
        assert raw.startswith(b"\xef\xbb\xbf"), "CSV must start with a UTF-8 BOM"
        text = raw.decode("utf-8-sig")
        assert "\r\n" in text, "CSV must use CRLF line endings"

        rows = list(csv.reader(io.StringIO(text)))
        header = rows[0]
        assert header[:6] == [
            "serial", "filename", "scan_timestamp", "processing_seconds", "pass_fail", "score",
        ]
        for field in CSV_FIELDS:
            assert f"{field}_verdict" in header, field
            assert f"{field}_reason" in header, field
        assert header[-1] == "error"
        assert all(len(row) == self.EXPECTED_COLUMNS for row in rows), [len(r) for r in rows]
        assert len(rows) == 3  # header + 2 labels

        assert rows[1][0] == "001" and rows[2][0] == "002"
        assert rows[1][1] == names[0]
        # scan_timestamp is ISO 8601 and parses.
        for row in rows[1:]:
            parsed = datetime.fromisoformat(row[2])
            assert parsed.year >= 2026
        # processing_seconds is numeric with one decimal (R2 audit drift fix).
        seconds_at = header.index("processing_seconds")
        for row in rows[1:]:
            assert CSV_SECONDS_RE.match(row[seconds_at]), row[seconds_at]
            assert 0.0 <= float(row[seconds_at]) < 60.0, row[seconds_at]
        assert rows[1][4] == "PASS" and rows[1][5] == "6/6"
        assert rows[2][4] == "FAIL"
        verdict_at = header.index("brand_verdict")
        assert rows[1][verdict_at] == "match"
        assert rows[2][verdict_at] == "mismatch"

    def test_export_without_csv_marks_fields_no_submittal_data(
        self, page, base_url, tmp_path
    ):
        page.goto(base_url + "/")
        page.set_input_files("#file-input", label_paths(1))
        page.click("#scan-button")
        wait_for_banner(page)

        rows = list(csv.reader(io.StringIO(
            download_worksheet_csv(page, tmp_path).decode("utf-8-sig")
        )))
        header = rows[0]
        assert rows[1][header.index("pass_fail")] == "REVIEW"
        assert rows[1][header.index("brand_verdict")] == "no_submittal_data"
        # The statutory warning verdict is real even without submittal data.
        assert rows[1][header.index("government_warning_verdict")] == "match"

    def test_formula_injection_filenames_stay_guarded(self, page, base_url, tmp_path):
        hostile_names = ["=HYPERLINK(A1).png", "+cmd-launch.png", "-2+3.png", "@SUM(A1).png"]
        page.goto(base_url + "/")
        page.set_input_files("#file-input", files=memory_files(hostile_names))
        page.click("#scan-button")
        wait_for_banner(page)

        raw = download_worksheet_csv(page, tmp_path)
        rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig"))))
        filenames = [row[1] for row in rows[1:]]
        for name in hostile_names:
            assert ("'" + name) in filenames, f"{name!r} not formula-guarded: {filenames}"
        for row in rows[1:]:
            for cell in row:
                assert not cell.startswith(("=", "+", "@")), f"unguarded cell {cell!r}"


def drop_on_csv_zone(page, files: list[dict]) -> None:
    """Synthesize a drag-and-drop onto the step-2 CSV dropzone: build a real
    DataTransfer with in-memory File objects and dispatch a drop event."""
    page.evaluate(
        """(files) => {
            const dt = new DataTransfer();
            for (const f of files) {
                dt.items.add(new File([f.content], f.name, { type: f.type }));
            }
            document.getElementById('csv-dropzone').dispatchEvent(
                new DragEvent('drop', { dataTransfer: dt, bubbles: true, cancelable: true })
            );
        }""",
        files,
    )


class TestCsvDropzoneDragDrop:
    """Owner feedback: step 2 mirrors the photo dropzone. Dropping a single
    supported form file (CSV/TSV/XLSX/PDF/photo) anywhere on the zone selects
    it (through the hidden #csv-input, which stays the source of truth);
    anything else gets the friendly error callout and selects nothing."""

    def test_dropping_a_csv_selects_it_and_the_scan_uses_it(self, page, base_url):
        page.goto(base_url + "/")
        page.set_input_files("#file-input", files=memory_files(["a.png"]))
        drop_on_csv_zone(page, [{
            "name": "dropped-submittal.csv",
            "type": "text/csv",
            "content": matching_manifest(["a.png"]),
        }])
        playwright_api.expect(page.locator("#csv-status")).to_contain_text(
            "dropped-submittal.csv"
        )
        playwright_api.expect(page.locator("#csv-dropzone-selected")).to_be_visible()
        playwright_api.expect(page.locator("#error-callout")).to_be_hidden()
        # The hidden input carries the dropped file — every set_input_files
        # consumer and the submit path read from the same place.
        assert page.evaluate(
            "document.getElementById('csv-input').files[0].name"
        ) == "dropped-submittal.csv"
        # And the scan really checks against the dropped spreadsheet.
        page.click("#scan-button")
        playwright_api.expect(page.locator("#banner-text")).to_have_text(
            "1 label scanned — 1 passed", timeout=15_000
        )

    def test_dropping_an_unsupported_file_shows_friendly_error_and_selects_nothing(
        self, page, base_url
    ):
        # PNG/JPG/PDF are valid FORM formats now (WP7) — an unsupported type
        # like a video still gets the friendly callout and selects nothing.
        page.goto(base_url + "/")
        drop_on_csv_zone(page, [{
            "name": "walkthrough.mp4", "type": "video/mp4", "content": "not a form",
        }])
        callout = page.locator("#error-callout")
        playwright_api.expect(callout).to_be_visible()
        playwright_api.expect(callout).to_contain_text("doesn't look like a submittal form")
        playwright_api.expect(page.locator("#csv-dropzone-selected")).to_be_hidden()
        assert page.evaluate("document.getElementById('csv-input').files.length") == 0

    def test_dropping_multiple_files_is_rejected(self, page, base_url):
        page.goto(base_url + "/")
        drop_on_csv_zone(page, [
            {"name": "one.csv", "type": "text/csv", "content": "filename,brand\n"},
            {"name": "two.csv", "type": "text/csv", "content": "filename,brand\n"},
        ])
        playwright_api.expect(page.locator("#error-callout")).to_be_visible()
        playwright_api.expect(page.locator("#csv-dropzone-selected")).to_be_hidden()
        assert page.evaluate("document.getElementById('csv-input').files.length") == 0

    def test_dropping_a_csv_replaces_the_previous_one(self, page, base_url):
        page.goto(base_url + "/")
        page.set_input_files(
            "#csv-input", files=csv_payload("filename,brand\n", name="first.csv")
        )
        playwright_api.expect(page.locator("#csv-status")).to_contain_text("first.csv")
        drop_on_csv_zone(page, [{
            "name": "second.csv", "type": "text/csv", "content": "filename,brand\n",
        }])
        playwright_api.expect(page.locator("#csv-status")).to_contain_text("second.csv")
        assert page.evaluate("document.getElementById('csv-input').files.length") == 1

    def test_choose_button_is_a_real_button_and_opens_the_file_picker(
        self, page, base_url
    ):
        page.goto(base_url + "/")
        assert page.evaluate(
            "document.querySelector('.csv-choose').tagName"
        ) == "BUTTON"
        with page.expect_file_chooser() as chooser_info:
            page.click("text=Choose form from your computer")
        chooser_info.value.set_files([{
            "name": "picked.csv", "mimeType": "text/csv",
            "buffer": b"filename,brand\n",
        }])
        playwright_api.expect(page.locator("#csv-status")).to_contain_text("picked.csv")
