"""QA gate 4 — adversarial probes of the NEW worksheet surfaces (WP5).

Small, sharp probes of surfaces that did not exist before the worksheet
redesign, plus findings from re-baselining the stale-state rules:

  * The drill-down for an error-entry row (no verdicts): friendly prose,
    no undefined/NaN text, no dangling elapsed-time fragment.
  * Manifest edge semantics through the UI: a photo listed twice in the CSV
    under case-differing filenames (structural 400, rendered inertly), and a
    CSV row with no matching photo (ignored — no ghost worksheet row).
  * Stale-state rules for the submittal-CSV input (test_qa4_finding_* —
    filed as failing findings, since fixed: snapshot-at-submit + clear on
    CSV change).

Same harness as test_qa2_e2e.py. Marked ``e2e``; skips cleanly when
Playwright/Chromium is unavailable. No API key, no network beyond localhost.
"""

from __future__ import annotations

import re

import pytest

playwright_api = pytest.importorskip(
    "playwright.sync_api", reason="playwright is not installed"
)

from app.extraction import ExtractionError
from tests.conftest import FakeExtractor
from tests.qa._qa_worksheet_harness import (
    GOOD_EXTRACTION,
    HOSTILE_FILENAME,
    MANIFEST_HEADER,
    SlowFakeExtractor,
    SwitchableExtractor,
    browser_page,
    csv_payload,
    memory_files,
    serve,
    wait_for_banner,
    worksheet_rows,
)

pytestmark = pytest.mark.e2e

MATCHING_ROW_BODY = (
    '"Stone\'s Throw","Kentucky Straight Bourbon Whiskey",45%,750 mL,'
    '"Blue Ridge Distilling Co., 12 Main Street, Asheville, NC 28801",,false'
)


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


class TestQa4ErrorEntryDrillDown:
    def test_qa4_error_row_drilldown_is_friendly_no_nan_no_timer_fragment(
        self, page, base_url, extractor
    ):
        extractor.delegate = FakeExtractor(
            error=ExtractionError("The label reading service is unavailable right now.")
        )
        page.goto(base_url + "/")
        page.set_input_files("#file-input", files=memory_files(["a.png"]))
        page.click("#scan-button")
        wait_for_banner(playwright_api, page)

        row = worksheet_rows(page).first
        playwright_api.expect(row.locator(".status-badge")).to_have_text("ERROR")
        # Error entries carry no processing_time_ms: the Time cell shows an
        # em-dash placeholder, never "NaNs"/"undefineds".
        time_cell = (row.locator('td[data-label="Time"]').text_content() or "").strip()
        assert time_cell == "—", f"error-row Time cell should be —, got {time_cell!r}"

        row.locator(".review-button").click()
        panel = page.locator(".detail-panel")
        playwright_api.expect(panel).to_be_visible()
        playwright_api.expect(panel.locator(".detail-error")).to_contain_text(
            "Couldn't scan this photo"
        )
        panel_text = panel.text_content() or ""
        assert "undefined" not in panel_text, "undefined leaked into the panel"
        assert "NaN" not in panel_text, "NaN leaked into the panel"
        # The header stamp has no dangling elapsed-time fragment ("· Ns").
        stamp = (panel.locator(".detail-stamp").text_content() or "").strip()
        assert re.fullmatch(r"Scanned \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", stamp), (
            f"error-entry stamp should carry no timer fragment: {stamp!r}"
        )
        # The detail row spans the full worksheet width (COLUMN_COUNT = 13).
        colspan = page.eval_on_selector(
            "#worksheet-body > tr.detail-row > td", "el => el.colSpan"
        )
        assert colspan == 13, f"detail row colspan {colspan} != 13"


class TestQa4ManifestEdgeSemantics:
    def test_qa4_case_differing_duplicate_csv_rows_fail_inertly(
        self, page, base_url
    ):
        """Two CSV rows whose filenames differ only by case collide after
        normalization -> structural 400 whose message echoes the (hostile)
        filename -> every row becomes an error entry, rendered inertly."""
        duplicate_rows = (
            "filename,brand\n"
            f"{HOSTILE_FILENAME},Brand One\n"
            f"{HOSTILE_FILENAME.upper()},Brand Two\n"
        )
        page.goto(base_url + "/")
        page.set_input_files("#file-input", files=memory_files([HOSTILE_FILENAME]))
        page.set_input_files("#csv-input", files=csv_payload(duplicate_rows))
        page.click("#scan-button")
        wait_for_banner(playwright_api, page)

        assert page.evaluate("window.__pwned") in (None, False)
        row = worksheet_rows(page).first
        playwright_api.expect(row.locator(".status-badge")).to_have_text("ERROR")
        playwright_api.expect(page.locator("#worksheet-body")).to_contain_text(
            "more than once"
        )
        inner = page.eval_on_selector("#worksheet-body", "el => el.innerHTML")
        assert "<img src=x" not in inner, "raw lowercase markup in the error cell"
        assert "<IMG SRC=X" not in inner, "raw uppercase markup in the error cell"
        assert page.evaluate(
            "document.querySelectorAll("
            "'#results img[src=\"x\"], #results img[src=\"X\"]').length"
        ) == 0

    def test_qa4_csv_row_with_no_matching_photo_is_reported_missing(self, page, base_url):
        """A manifest row for a photo that was never uploaded is REPORTED, not
        silently dropped: it appears as a flagged MISSING worksheet row and the
        banner counts it (behavior changed from ignore -> report in a9a6260)."""
        manifest = (
            MANIFEST_HEADER + "\n"
            f"a.png,{MATCHING_ROW_BODY}\n"
            f"ghost.png,{MATCHING_ROW_BODY}\n"
        )
        page.goto(base_url + "/")
        page.set_input_files("#file-input", files=memory_files(["a.png"]))
        page.set_input_files("#csv-input", files=csv_payload(manifest))
        page.click("#scan-button")
        wait_for_banner(playwright_api, page)

        playwright_api.expect(page.locator("#banner-text")).to_have_text(
            "1 label scanned — 1 passed, 1 form row had no photo"
        )
        playwright_api.expect(worksheet_rows(page)).to_have_count(2)
        body_text = page.locator("#worksheet-body").text_content() or ""
        assert "ghost.png" in body_text, "missing form row absent from the worksheet"
        badges = page.eval_on_selector_all(
            "#worksheet-body .status-badge", "els => els.map(e => e.textContent)"
        )
        assert badges.count("MISSING") == 1, badges


class TestQa4CsvStaleStateFindings:
    """FINDINGS — the submittal-CSV input is exempt from the stale-state rules
    the photo input honors. These tests assert the CORRECT behavior and are
    EXPECTED TO FAIL until the build agent fixes app/static/app.js. Do not
    weaken them to make the suite green (TESTING.md: a QA finding is either
    FIXED or documented in APPROACH.md as a known limitation)."""

    def test_qa4_finding_new_csv_after_results_leaves_stale_worksheet(
        self, page, base_url
    ):
        """QA4-F1 (LOW): selecting NEW PHOTOS after a completed scan clears the
        old worksheet (WP5 stale-state rule), but selecting a DIFFERENT
        SUBMITTAL CSV does not — verdicts computed against the old CSV stay on
        screen next to the newly chosen spreadsheet, implying they reflect it."""
        page.goto(base_url + "/")
        page.set_input_files("#file-input", files=memory_files(["a.png"]))
        page.set_input_files(
            "#csv-input", files=csv_payload(f"{MANIFEST_HEADER}\na.png,{MATCHING_ROW_BODY}\n")
        )
        page.click("#scan-button")
        wait_for_banner(playwright_api, page)

        # Swap in a different submittal CSV WITHOUT re-scanning.
        page.set_input_files(
            "#csv-input",
            files=csv_payload(
                "filename,brand\na.png,Completely Different Brand\n",
                name="corrected-submittal.csv",
            ),
        )
        assert page.locator("#results").is_visible() is False, (
            "stale worksheet (verdicts for the OLD submittal CSV) remains on "
            "screen after choosing a different CSV — same class of bug as the "
            "fixed new-photos stale-state finding"
        )

    def test_qa4_finding_removing_csv_mid_scan_swaps_application_data(
        self, page, base_url, extractor
    ):
        """QA4-F2 (MEDIUM): sendChunk reads the LIVE csvFile variable while
        hasSubmittal is latched at submit time. Clicking 'Remove the
        spreadsheet' mid-scan makes later chunks fall back to the placeholder
        brand "-" — those rows then render REAL-looking verdicts (e.g. brand
        'Doesn't match' against submittal value '-') instead of the scan's
        actual submittal data. A verdict surface must never silently swap its
        comparison basis mid-scan."""
        extractor.delegate = SlowFakeExtractor(GOOD_EXTRACTION, delay=0.4)
        names = [f"l{i:02d}.png" for i in range(12)]  # 2 chunks: 10 + 2
        manifest = MANIFEST_HEADER + "\n" + "".join(
            f"{name},{MATCHING_ROW_BODY}\n" for name in names
        )
        page.goto(base_url + "/")
        page.set_input_files("#file-input", files=memory_files(names))
        page.set_input_files("#csv-input", files=csv_payload(manifest))
        page.click("#scan-button")

        # Remove the spreadsheet while chunk 1 is still in flight.
        page.click("#csv-clear")
        assert page.locator("#progress-block").is_visible(), (
            "timing guard: the scan already finished before the CSV was "
            "removed — the probe did not exercise the mid-scan path"
        )
        wait_for_banner(playwright_api, page, timeout=30_000)
        playwright_api.expect(worksheet_rows(page)).to_have_count(12)

        # Row 11 arrived in chunk 2 (after the removal). Its comparison basis
        # must still be the submittal data this scan was started with — never
        # the silent "-" placeholder.
        row_11 = worksheet_rows(page).nth(10)
        row_11.locator(".review-button").click()
        panel = page.locator(".detail-panel")
        playwright_api.expect(panel).to_be_visible()
        brand_row = panel.locator(".detail-table tr", has_text="Brand name").first
        submittal_says = (brand_row.locator("td").nth(1).text_content() or "").strip()
        assert submittal_says != "-", (
            "row scanned after mid-scan CSV removal was verified against the "
            "placeholder brand '-' instead of the submittal data the scan "
            "started with"
        )
