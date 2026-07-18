"""QA gate 2 — adversarial browser E2E of the worksheet UI, single-photo flow.

RE-BASELINED for the WP5 worksheet redesign (the old tab/typed-form DOM —
#check-button, #brand, #results-body — no longer exists). Every security/UX
property the old suite pinned survives here in worksheet form:

  * SECURITY — extraction output is UNTRUSTED. Prove by ATTACK that hostile
    markup in every rendered surface (the 7 field cells, the reason prose and
    clause diff inside the drill-down panel, the value-truncation title path,
    and the per-label error text) renders as INERT TEXT and never executes.
  * ADVERSARIAL UX — double-fire Scan, stale-worksheet clearing on new photo
    selection, na ("Not checked") rendering, the CSV is_import gate (the
    worksheet replacement for the old import checkbox), and the focus
    contract (banner on completion, panel on open, button on Escape).

Independent of the build agent's tests/test_e2e_ui.py. Boots the real FastAPI
app on a loopback port with the extractor overridden to a switchable fake and
drives headless Chromium. Marked ``e2e``; skips cleanly when
Playwright/Chromium is unavailable. No API key, no network beyond localhost.
"""

from __future__ import annotations

import time
from dataclasses import replace

import pytest

playwright_api = pytest.importorskip(
    "playwright.sync_api", reason="playwright is not installed"
)

from app.extraction import ExtractionError
from tests.conftest import FakeExtractor
from tests.qa._qa_worksheet_harness import (
    GOOD_EXTRACTION,
    HOSTILE_EXTRACTION,
    MANIFEST_HEADER,
    XSS_IMG,
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

# A >60-char value carrying a payload, to exercise the worksheet cell
# truncation + title-attribute path (app.js shorten()/td.title).
LONG_HOSTILE = XSS_IMG + " " + ("A" * 200)

# A submittal row for label.png that fills EVERY column (import checked), so
# all seven matchers run and every rendered surface receives hostile input.
FULL_ROW = "label.png,Legit Brand,Bourbon,45%,750 mL,Distiller Co.,France,true"


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


def scan_one(page, base_url: str, manifest: str | None = None) -> None:
    """Load the page, attach one in-memory photo (label.png), optionally a
    submittal CSV, and click Scan Labels."""
    page.goto(base_url + "/")
    page.set_input_files("#file-input", files=memory_files(["label.png"]))
    if manifest is not None:
        page.set_input_files("#csv-input", files=csv_payload(manifest))
    page.click("#scan-button")


class TestXSSInEveryRenderedSurface:
    """The build agent CLAIMS all dynamic text uses textContent. Verify by attack."""

    def test_qa2_hostile_extraction_renders_inert_in_worksheet_and_panel(
        self, page, base_url, extractor
    ):
        extractor.delegate = FakeExtractor(HOSTILE_EXTRACTION)
        # Full submittal row -> every matcher runs, every field cell renders.
        scan_one(page, base_url, manifest=f"{MANIFEST_HEADER}\n{FULL_ROW}\n")
        wait_for_banner(playwright_api, page)

        # 1) No payload executed anywhere.
        assert page.evaluate("window.__pwned") in (None, False), "XSS EXECUTED — pwned"

        # 2) The field cells show the markup as LITERAL TEXT (escaped, not parsed).
        body = page.locator("#worksheet-body")
        playwright_api.expect(body).to_contain_text("<img src=x onerror=")
        inner = page.eval_on_selector("#worksheet-body", "el => el.innerHTML")
        assert "<img src=x" not in inner, "raw <img> injected into the worksheet"
        assert "&lt;img" in inner, "hostile text was not HTML-escaped in the DOM"

        # 3) No attacker element materialized in the results subtree (the
        #    legitimate thumbnails are img.thumb; attacker's would be img[src=x]).
        injected = page.evaluate(
            "document.querySelectorAll('#results img[src=\"x\"], #results script').length"
        )
        assert injected == 0, "attacker <img>/<script> element was created"

        # 4) The row itself stays structurally sound: all 13 worksheet columns.
        assert worksheet_rows(page).first.locator("td").count() == 13

        # 5) Drill-down: reasons, the comparison table, and the clause-diff
        #    `found` text all receive hostile strings — still inert.
        page.locator(".review-button").first.click()
        panel = page.locator(".detail-panel")
        playwright_api.expect(panel).to_be_visible()
        diff = panel.locator(".clause-diff")
        playwright_api.expect(diff).to_be_visible()
        playwright_api.expect(diff).to_contain_text("Label says:")
        results_html = page.eval_on_selector("#results", "el => el.innerHTML")
        assert "<script>window.__pwned" not in results_html, "raw <script> in panel"
        assert "<img src=x" not in results_html, "raw <img> in panel"
        assert page.evaluate("window.__pwned") in (None, False), "XSS on panel open"

    def test_qa2_truncation_title_path_is_inert(self, page, base_url, extractor):
        extractor.delegate = FakeExtractor(
            replace(GOOD_EXTRACTION, producer=LONG_HOSTILE)
        )
        scan_one(page, base_url)
        wait_for_banner(playwright_api, page)
        assert page.evaluate("window.__pwned") in (None, False)
        # The >60-char value is truncated with a title attribute holding the
        # full text as an ATTRIBUTE STRING (never parsed as HTML).
        title = page.evaluate(
            "Array.from(document.querySelectorAll('#worksheet-body td.field-cell'))"
            ".map(td => td.getAttribute('title')).find(t => t && t.indexOf('onerror') !== -1)"
        )
        assert title is not None and "<img" in title, "title should hold literal markup"
        assert page.evaluate("window.__pwned") in (None, False)

    def test_qa2_hostile_error_message_renders_inert_in_row_and_panel(
        self, page, base_url, extractor
    ):
        # A hostile ExtractionError message flows: server error.message -> batch
        # error entry -> worksheet error cell AND the drill-down error callout.
        extractor.delegate = FakeExtractor(error=ExtractionError(f"Reader down {XSS_IMG}"))
        scan_one(page, base_url)
        wait_for_banner(playwright_api, page)
        assert page.evaluate("window.__pwned") in (None, False)

        row = worksheet_rows(page).first
        playwright_api.expect(row.locator(".status-badge")).to_have_text("ERROR")
        playwright_api.expect(row).to_contain_text("Reader down")
        playwright_api.expect(row).to_contain_text("<img")
        inner = page.eval_on_selector("#worksheet-body", "el => el.innerHTML")
        assert "<img src=x" not in inner, "raw markup in the error row"

        row.locator(".review-button").click()
        panel = page.locator(".detail-panel")
        playwright_api.expect(panel).to_be_visible()
        playwright_api.expect(panel.locator(".detail-error")).to_contain_text("Reader down")
        panel_html = page.eval_on_selector(".detail-panel", "el => el.innerHTML")
        assert "<img src=x" not in panel_html, "raw markup in the panel error callout"
        assert page.evaluate("window.__pwned") in (None, False)

    def test_qa2_thumbnail_and_detail_image_use_object_urls(self, page, base_url):
        scan_one(page, base_url)
        wait_for_banner(playwright_api, page)
        thumb_src = worksheet_rows(page).first.locator(".thumb").get_attribute("src")
        assert thumb_src and thumb_src.startswith("blob:"), (
            f"thumbnail src should be a blob: URL, got {thumb_src!r}"
        )
        page.locator(".review-button").first.click()
        detail_src = page.locator(".detail-panel .detail-figure img").get_attribute("src")
        assert detail_src and detail_src.startswith("blob:"), (
            f"detail image src should be a blob: URL, got {detail_src!r}"
        )


class TestAdversarialSubmit:
    def test_qa2_rapid_double_click_fires_exactly_one_request_sequence(
        self, page, base_url, extractor
    ):
        # A slow extractor keeps the scan in flight across the second click.
        extractor.delegate = SlowFakeExtractor(GOOD_EXTRACTION, delay=0.4)
        page.goto(base_url + "/")
        page.set_input_files("#file-input", files=memory_files(["label.png"]))
        calls = {"n": 0}

        def count(req):
            if req.url.endswith("/api/verify-batch"):
                calls["n"] += 1

        page.on("request", count)
        try:
            page.dblclick("#scan-button")
            # The button must be disabled while the scan is in flight.
            playwright_api.expect(page.locator("#scan-button")).to_be_disabled()
            wait_for_banner(playwright_api, page)
            time.sleep(0.3)  # let any stray second request land
            assert calls["n"] == 1, (
                f"submit double-fired: {calls['n']} POSTs to /api/verify-batch"
            )
        finally:
            page.remove_listener("request", count)
        playwright_api.expect(page.locator("#scan-button")).to_be_enabled()

    def test_qa2_blank_submittal_fields_render_not_checked_not_blank(
        self, page, base_url
    ):
        # The CSV provides only the (required) brand: the five other
        # submittal-checked columns must read "Not checked" — never blank,
        # and never a silent match.
        scan_one(page, base_url, manifest="filename,brand\nlabel.png,Stone's Throw\n")
        wait_for_banner(playwright_api, page)
        row = worksheet_rows(page).first
        na_marks = row.locator(".mark-na")
        # class_type, abv, net_contents, producer + origin (domestic) = 5.
        assert na_marks.count() == 5, "expected 5 'Not checked' cells"
        for i in range(na_marks.count()):
            playwright_api.expect(na_marks.nth(i)).to_have_attribute(
                "title", "Not checked"
            )
        # Brand genuinely matched; the row passes on the applicable fields.
        playwright_api.expect(
            row.locator('td[data-label="Brand name"] .mark')
        ).to_have_attribute("title", "Matches")

    def test_qa2_import_false_ignores_csv_origin_value(self, page, base_url):
        # Worksheet replacement for the old import-checkbox test: a CSV row
        # carrying an origin_country while is_import=false must verify as
        # domestic N/A ("Not checked"), NOT as a stale France comparison.
        manifest = f"{MANIFEST_HEADER}\nlabel.png,Stone's Throw,,,,,France,false\n"
        scan_one(page, base_url, manifest=manifest)
        wait_for_banner(playwright_api, page)
        origin_mark = worksheet_rows(page).first.locator(
            'td[data-label="Country of origin"] .mark'
        )
        playwright_api.expect(origin_mark).to_have_attribute("title", "Not checked")

    def test_qa2_new_photos_after_results_clear_stale_worksheet(self, page, base_url):
        # First run: a completed scan with a visible worksheet + banner.
        scan_one(page, base_url)
        wait_for_banner(playwright_api, page)
        # Now choose different photos WITHOUT re-scanning. The old worksheet
        # (old verdicts, old banner) must not sit next to the new selection.
        page.set_input_files("#file-input", files=memory_files(["different.png"]))
        assert page.locator("#results").is_visible() is False, (
            "stale worksheet/banner remain visible after selecting new photos"
        )


class TestFocusContract:
    def test_qa2_focus_banner_then_panel_then_escape_returns_to_button(
        self, page, base_url
    ):
        scan_one(page, base_url)
        wait_for_banner(playwright_api, page)
        # Completion moves focus to the summary banner (screen readers announce).
        assert page.evaluate(
            "document.activeElement && document.activeElement.id"
        ) == "banner", "focus must land on the banner when the scan completes"

        # Opening the drill-down moves focus into the panel.
        button = page.locator(".review-button").first
        button.click()
        playwright_api.expect(page.locator(".detail-panel")).to_be_visible()
        assert page.evaluate(
            "document.activeElement && document.activeElement.className"
        ).startswith("detail-panel")
        playwright_api.expect(button).to_have_attribute("aria-expanded", "true")

        # Escape closes the panel and returns focus to the row's button.
        page.keyboard.press("Escape")
        playwright_api.expect(page.locator(".detail-panel")).to_have_count(0)
        assert page.evaluate(
            "document.activeElement && document.activeElement.className"
        ).endswith("review-button")
        playwright_api.expect(button).to_have_attribute("aria-expanded", "false")

    def test_qa2_review_control_is_a_real_button_and_keyboard_operable(
        self, page, base_url
    ):
        scan_one(page, base_url)
        wait_for_banner(playwright_api, page)
        assert page.evaluate("document.querySelector('.review-button').tagName") == "BUTTON"
        page.locator(".review-button").first.focus()
        page.keyboard.press("Enter")
        playwright_api.expect(page.locator(".detail-panel")).to_be_visible()
