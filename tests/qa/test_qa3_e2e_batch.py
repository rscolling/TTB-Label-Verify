"""QA gate 3 — adversarial browser E2E of the worksheet's multi-photo +
submittal-CSV flow and the client-side CSV export.

RE-BASELINED for the WP5 worksheet redesign (the old batch tab —
#tab-batch, #batch-results-body, #batch-download — no longer exists).
Preserved properties, in worksheet form:

  * SECURITY — extraction output AND uploaded file names are UNTRUSTED; so is
    the submittal CSV's own file name (rendered into the #csv-status line).
    Prove by attack that every rendered surface stays inert: worksheet rows,
    per-label error entries (missing manifest row), and the whole-chunk
    failure path (structural manifest error echoed into every row).
  * CSV EXPORT — formula-injection guard + structure under hostile values,
    downloaded through the real browser and re-parsed with Python's csv
    module; the NEW columns (serial, scan_timestamp, processing_seconds,
    pass_fail, score) must survive hostile data intact.
  * UX — worksheet state survives opening/closing the drill-down (the analog
    of the old tab-switch state test), and the 390px stacked-card rendering
    (data-label pattern) holds with the new Serial/Scanned at/Time columns.

Same harness as test_qa2_e2e.py. Marked ``e2e``; skips cleanly when
Playwright/Chromium is unavailable. No API key, no network beyond localhost.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import datetime

import pytest

playwright_api = pytest.importorskip(
    "playwright.sync_api", reason="playwright is not installed"
)

from app.extraction import ExtractionError
from tests.conftest import FakeExtractor
from tests.qa._qa_worksheet_harness import (
    GOOD_EXTRACTION,
    HOSTILE_EXTRACTION,
    HOSTILE_FILENAME,
    MANIFEST_HEADER,
    XSS_IMG,
    SwitchableExtractor,
    browser_page,
    csv_payload,
    download_via,
    memory_files,
    serve,
    wait_for_banner,
    worksheet_rows,
)

pytestmark = pytest.mark.e2e

SERIAL_RE = re.compile(r"^\d{3}$")
STAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
ELAPSED_RE = re.compile(r"^\d+\.\ds$")
ISO_STAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")
CSV_SECONDS_RE = re.compile(r"^\d+\.\d$")

CSV_FIELDS = [
    "brand", "class_type", "abv", "net_contents",
    "producer", "origin_country", "government_warning",
]
# serial, filename, scan_timestamp, processing_seconds, pass_fail, score,
# 7 x (verdict, reason), error
EXPECTED_COLUMNS = 6 + 7 * 2 + 1

# A submittal row body filling every column, import checked.
FULL_ROW_BODY = "Legit Brand,Bourbon,45%,750 mL,Distiller Co.,France,true"


@pytest.fixture(scope="module")
def extractor() -> SwitchableExtractor:
    return SwitchableExtractor()


@pytest.fixture(autouse=True)
def reset_extractor(extractor: SwitchableExtractor):
    extractor.delegate = FakeExtractor(GOOD_EXTRACTION)
    yield


@pytest.fixture(scope="module")
def base_url(extractor: SwitchableExtractor):
    yield from serve(extractor)


@pytest.fixture(scope="module")
def page(base_url):
    yield from browser_page(playwright_api)


def manifest_for(filenames: list[str]) -> str:
    return MANIFEST_HEADER + "\n" + "".join(
        f"{name},{FULL_ROW_BODY}\n" for name in filenames
    )


def scan(page, base_url: str, names: list[str], manifest: str | None = None,
         manifest_name: str = "submittal.csv") -> None:
    page.goto(base_url + "/")
    page.set_input_files("#file-input", files=memory_files(names))
    if manifest is not None:
        page.set_input_files(
            "#csv-input", files=csv_payload(manifest, name=manifest_name)
        )
    page.click("#scan-button")


def assert_row_bookkeeping_cells_well_formed(page, count: int) -> None:
    """Serial zero-padded, timestamp formatted, Time cell 'N.Ns' — on every row."""
    for i in range(count):
        row = worksheet_rows(page).nth(i)
        serial = (row.locator('td[data-label="Serial"]').text_content() or "")
        serial = serial.replace("⚑", "").strip()
        assert SERIAL_RE.match(serial), f"row {i} serial {serial!r}"
        assert int(serial) == i + 1, f"row {i} serial out of order: {serial!r}"
        stamp = (row.locator('td[data-label="Scanned at"]').text_content() or "").strip()
        assert STAMP_RE.match(stamp), f"row {i} stamp {stamp!r}"
        elapsed = (row.locator('td[data-label="Time"]').text_content() or "").strip()
        assert ELAPSED_RE.match(elapsed), f"row {i} Time cell {elapsed!r}"


class TestQa3WorksheetXSS:
    def test_qa3_hostile_values_and_filenames_render_inert_in_worksheet(
        self, page, base_url, extractor
    ):
        extractor.delegate = FakeExtractor(HOSTILE_EXTRACTION)
        hostile_csv_name = "submittal <img src=x onerror=window.__pwned=1>.csv"
        scan(
            page,
            base_url,
            [HOSTILE_FILENAME, "normal.png"],
            manifest=manifest_for([HOSTILE_FILENAME, "normal.png"]),
            manifest_name=hostile_csv_name,
        )

        # The submittal CSV's own file name is rendered into #csv-status — inert.
        status_html = page.eval_on_selector("#csv-status", "el => el.innerHTML")
        assert "<img src=x" not in status_html, "raw markup in the CSV status line"
        assert "&lt;img" in status_html, "CSV file name was not escaped"

        wait_for_banner(playwright_api, page)

        # 1) Nothing executed.
        assert page.evaluate("window.__pwned") in (None, False), "XSS EXECUTED — pwned"

        # 2) Hostile FILENAME and hostile VALUES appear as literal text only.
        body = page.locator("#worksheet-body")
        playwright_api.expect(body).to_contain_text("<img src=x onerror=")
        inner = page.eval_on_selector("#worksheet-body", "el => el.innerHTML")
        assert "<img src=x" not in inner, "raw <img> markup injected into the worksheet"
        assert "&lt;img" in inner, "hostile text was not HTML-escaped in the DOM"
        injected = page.evaluate(
            "document.querySelectorAll('#results img[src=\"x\"], #results script').length"
        )
        assert injected == 0, "attacker <img>/<script> element was created"

        # 3) The bookkeeping columns stay well-formed next to hostile data.
        playwright_api.expect(worksheet_rows(page)).to_have_count(2)
        assert_row_bookkeeping_cells_well_formed(page, 2)

        # 4) Drill-down on the hostile row — comparison table + clause diff inert.
        page.locator(".review-button").first.click()
        playwright_api.expect(page.locator(".detail-panel")).to_be_visible()
        results_html = page.eval_on_selector("#results", "el => el.innerHTML")
        assert "<script>window.__pwned" not in results_html, "raw <script> in panel"
        assert "<img src=x" not in results_html, "raw <img> in panel"
        assert page.evaluate("window.__pwned") in (None, False), "XSS on panel open"

    def test_qa3_missing_manifest_row_error_carries_hostile_filename_inert(
        self, page, base_url
    ):
        """A photo with no manifest row gets a per-label error whose message
        embeds the (hostile) filename — the worksheet error cell stays inert."""
        scan(
            page,
            base_url,
            [HOSTILE_FILENAME],
            manifest="filename,brand\nsomething-else.png,Some Brand\n",
        )
        wait_for_banner(playwright_api, page)

        assert page.evaluate("window.__pwned") in (None, False)
        body = page.locator("#worksheet-body")
        playwright_api.expect(body).to_contain_text("doesn't have a row for")
        playwright_api.expect(
            worksheet_rows(page).first.locator(".status-badge")
        ).to_have_text("ERROR")
        inner = page.eval_on_selector("#worksheet-body", "el => el.innerHTML")
        assert "<img src=x" not in inner
        assert page.evaluate(
            "document.querySelectorAll('#results img[src=\"x\"]').length"
        ) == 0

    def test_qa3_structural_manifest_error_with_payload_renders_inert(
        self, page, base_url
    ):
        """A bad is_import value is echoed back verbatim in the 400 message;
        the chunk-failure path paints it into every row's error cell. Inert?"""
        scan(
            page,
            base_url,
            ["a.png"],
            manifest=f"filename,brand,is_import\na.png,Brand,oui{XSS_IMG}\n",
        )
        wait_for_banner(playwright_api, page)

        assert page.evaluate("window.__pwned") in (None, False)
        body = page.locator("#worksheet-body")
        playwright_api.expect(body).to_contain_text("is_import")
        playwright_api.expect(
            worksheet_rows(page).first.locator(".status-badge")
        ).to_have_text("ERROR")
        inner = page.eval_on_selector("#worksheet-body", "el => el.innerHTML")
        assert "<img src=x" not in inner
        assert page.evaluate(
            "document.querySelectorAll('#results img[src=\"x\"]').length"
        ) == 0


class TestQa3CsvExportAttacks:
    def test_qa3_formula_injection_filenames_guarded_and_new_columns_intact(
        self, page, base_url, tmp_path
    ):
        hostile_names = [
            "=HYPERLINK(A1).png",
            "+cmd-launch.png",
            "-2+3.png",
            "@SUM(A1).png",
            "comma,semicolon;.png",
            "wine🍷émoji.png",
            'he said "hi".png',
        ]
        scan(page, base_url, hostile_names)  # no CSV -> every row no-data REVIEW
        wait_for_banner(playwright_api, page)

        raw = download_via(page, "#download-csv", tmp_path, "qa3-worksheet.csv")

        # Excel-opens-cleanly proxy: BOM, CRLF row endings.
        assert raw.startswith(b"\xef\xbb\xbf"), "CSV must start with a UTF-8 BOM"
        text = raw.decode("utf-8-sig")
        assert "\r\n" in text, "CSV must use CRLF line endings"

        rows = list(csv.reader(io.StringIO(text)))
        header = rows[0]
        assert header[:6] == [
            "serial", "filename", "scan_timestamp", "processing_seconds",
            "pass_fail", "score",
        ]
        for field in CSV_FIELDS:
            assert f"{field}_verdict" in header, field
            assert f"{field}_reason" in header, field
        assert header[-1] == "error"
        assert len(rows) == 1 + len(hostile_names)
        assert all(len(row) == EXPECTED_COLUMNS for row in rows), (
            "inconsistent field count: " + repr([len(r) for r in rows])
        )

        filenames = [row[1] for row in rows[1:]]
        # Formula-leading filenames come back neutralized with a leading '.
        for name in ("=HYPERLINK(A1).png", "+cmd-launch.png", "-2+3.png", "@SUM(A1).png"):
            assert ("'" + name) in filenames, f"{name!r} not formula-guarded: {filenames}"
        for row in rows[1:]:
            for cell in row:
                assert not cell.startswith(("=", "+", "@")), f"unguarded cell {cell!r}"
        # Comma/semicolon, quotes, and non-ASCII names survive quoting intact.
        assert "comma,semicolon;.png" in filenames
        assert "wine🍷émoji.png" in filenames
        assert 'he said "hi".png' in filenames

        # NEW columns survive hostile adjacent data.
        seconds_at = header.index("processing_seconds")
        brand_verdict_at = header.index("brand_verdict")
        for i, row in enumerate(rows[1:], start=1):
            assert row[0] == f"{i:03d}", f"serial not zero-padded/sequential: {row[0]!r}"
            assert ISO_STAMP_RE.match(row[2]), f"scan_timestamp malformed: {row[2]!r}"
            assert datetime.fromisoformat(row[2]).year >= 2026
            assert CSV_SECONDS_RE.match(row[seconds_at]), row[seconds_at]
            assert 0.0 <= float(row[seconds_at]) < 60.0, row[seconds_at]
            assert row[4] == "REVIEW"  # no submittal data — never a silent pass
            assert row[5] == ""        # no fabricated score without submittal data
            assert row[brand_verdict_at] == "no_submittal_data"

    def test_qa3_error_message_with_newline_and_quotes_survives_export(
        self, page, base_url, extractor, tmp_path
    ):
        extractor.delegate = FakeExtractor(
            error=ExtractionError('Reader down\nline two, "quoted" and =SUM(A1)')
        )
        scan(page, base_url, ["a.png", "b.png"])
        wait_for_banner(playwright_api, page)

        raw = download_via(page, "#download-csv", tmp_path, "qa3-errors.csv")
        rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig"))))
        assert len(rows) == 3  # header + 2 labels — embedded newline did NOT split rows
        assert all(len(row) == EXPECTED_COLUMNS for row in rows)
        header = rows[0]
        error_at = header.index("error")
        seconds_at = header.index("processing_seconds")
        for row in rows[1:]:
            assert row[header.index("pass_fail")] == "ERROR"
            assert row[seconds_at] == "", "error entries have no processing time"
            assert ISO_STAMP_RE.match(row[2]), row[2]
            cell = row[error_at]
            assert "Reader down\nline two" in cell, "newline inside the cell was lost"
            assert '"quoted"' in cell, "double quotes were mangled"
            assert "=SUM(A1)" in cell  # mid-cell = is fine; only leading = is a formula

    def test_qa3_hostile_extraction_reasons_survive_export_reparse(
        self, page, base_url, extractor, tmp_path
    ):
        # Hostile extracted values flow into the verdict/reason columns; the
        # CSV structure and the new bookkeeping columns must hold.
        extractor.delegate = FakeExtractor(HOSTILE_EXTRACTION)
        scan(page, base_url, ["label.png"], manifest=manifest_for(["label.png"]))
        wait_for_banner(playwright_api, page)

        raw = download_via(page, "#download-csv", tmp_path, "qa3-hostile.csv")
        rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig"))))
        assert len(rows) == 2
        assert all(len(row) == EXPECTED_COLUMNS for row in rows)
        header = rows[0]
        row = rows[1]
        assert row[0] == "001"
        assert row[header.index("pass_fail")] == "FAIL"
        assert re.match(r"^\d+/\d+$", row[header.index("score")]), row
        assert row[header.index("brand_verdict")] == "mismatch"
        for cell in row:
            assert not cell.startswith(("=", "+", "@")), f"unguarded cell {cell!r}"


class TestQa3WorksheetUxRegression:
    def test_qa3_drilldown_open_close_preserves_worksheet_state(self, page, base_url):
        """Analog of the old tab-switch test: interacting with the drill-down
        must not destroy or mutate the worksheet results."""
        scan(page, base_url, ["a.png", "b.png"],
             manifest=manifest_for(["a.png", "b.png"]))
        wait_for_banner(playwright_api, page)
        banner_before = page.locator("#banner-text").text_content()

        page.locator(".review-button").first.click()
        playwright_api.expect(page.locator(".detail-panel")).to_be_visible()
        page.keyboard.press("Escape")
        playwright_api.expect(page.locator(".detail-panel")).to_have_count(0)

        playwright_api.expect(worksheet_rows(page)).to_have_count(2)
        assert page.locator("#banner-text").text_content() == banner_before
        playwright_api.expect(page.locator("#results")).to_be_visible()

    def test_qa3_390px_stacked_cards_hold_with_the_new_columns(self, page, base_url):
        """R5: at 390px the worksheet collapses to stacked cards announcing
        each column via data-label — including the new Serial / Scanned at /
        Time columns — with no horizontal page scrolling."""
        original = page.viewport_size or {"width": 1280, "height": 720}
        try:
            page.set_viewport_size({"width": 390, "height": 844})
            scan(page, base_url, ["a.png"], manifest=manifest_for(["a.png"]))
            wait_for_banner(playwright_api, page)

            # No horizontal page scrolling.
            assert page.evaluate(
                "document.documentElement.scrollWidth <= window.innerWidth + 1"
            ), "page scrolls horizontally at 390px"

            # Table header collapses; rows become block-level cards.
            assert page.eval_on_selector(
                "#worksheet thead", "el => getComputedStyle(el).display"
            ) == "none"
            assert page.eval_on_selector(
                "#worksheet-body > tr.worksheet-row",
                "el => getComputedStyle(el).display",
            ) == "block"

            # Every stacked cell announces its column via the data-label
            # ::before pattern — spot-check the new columns + a field column.
            for label in ("Serial", "Scanned at", "Time", "Brand name", "Score"):
                content = page.eval_on_selector(
                    f'td[data-label="{label}"]',
                    "el => getComputedStyle(el, '::before').content",
                )
                assert label in (content or ""), (
                    f"data-label {label!r} not announced at 390px: {content!r}"
                )
        finally:
            page.set_viewport_size(original)
