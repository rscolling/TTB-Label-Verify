"""QA gate 5 — matching edge cases + snapshot-at-submit through the UI (WP7).

Drives the real browser with a faked form extractor to exercise the client's
order/name matching rules (buildScanPlan) at their boundaries:

  * no-filename form with FEWER and with MORE rows than photos -> order-match
    what pairs, extras scan without submittal data (REVIEW) and leftover rows
    are reported MISSING (never a silent mispairing or drop);
  * mixed named/unnamed rows where the leftover counts are ambiguous ->
    unnamed rows reported MISSING, unmatched photos scanned without submittal
    data (flagged), with the notice explaining it;
  * duplicate filenames in ingested rows that differ only by case -> blocked
    with the duplicate-photo message (the form's own duplicate warning is also
    surfaced at ingest time);
  * the form re-ingested / removed MID-SCAN -> the snapshot taken at submit
    holds (extends the gate-4 snapshot finding pattern to the ingested-rows
    path).

Same harness as the other QA e2e suites. Marked ``e2e``.
"""

from __future__ import annotations

import pytest

playwright_api = pytest.importorskip(
    "playwright.sync_api", reason="playwright is not installed"
)

from dataclasses import replace

from app.form_ingest import FormRow
from tests.conftest import FakeExtractor, FakeFormExtractor
from tests.qa._qa_worksheet_harness import (
    GOOD_EXTRACTION,
    SlowFakeExtractor,
    SwitchableExtractor,
    SwitchableFormExtractor,
    browser_page,
    memory_files,
    pdf_payload,
    serve,
    wait_for_banner,
    wait_for_ingest,
    worksheet_rows,
)

pytestmark = pytest.mark.e2e

FULL_ROW = dict(
    class_type="Kentucky Straight Bourbon Whiskey",
    abv="45%",
    net_contents="750 mL",
    producer="Blue Ridge Distilling Co., 12 Main Street, Asheville, NC 28801",
)


def full_row(brand="Stone's Throw", filename=None, **kw):
    return FormRow(filename=filename, brand=brand, **{**FULL_ROW, **kw})


@pytest.fixture(scope="module")
def extractor() -> SwitchableExtractor:
    return SwitchableExtractor()


@pytest.fixture(scope="module")
def form_extractor() -> SwitchableFormExtractor:
    return SwitchableFormExtractor()


@pytest.fixture(autouse=True)
def reset_extractors(extractor, form_extractor):
    extractor.delegate = FakeExtractor(GOOD_EXTRACTION)
    form_extractor.delegate = FakeFormExtractor()
    yield


@pytest.fixture(scope="module")
def base_url(extractor, form_extractor):
    yield from serve(extractor, form_extractor)


@pytest.fixture(scope="module")
def page(base_url):
    yield from browser_page(playwright_api)


class TestQa5CountMismatch:
    """Count mismatches no longer block (behavior changed in a9a6260): rows
    and photos pair by order, extra photos scan without submittal data
    (flagged REVIEW), and leftover form rows become flagged MISSING report
    rows — never a silent drop or mispairing."""

    def test_qa5_fewer_form_rows_than_photos_order_matches_and_flags_extras(
        self, page, base_url, form_extractor
    ):
        form_extractor.delegate = FakeFormExtractor(rows=[full_row(), full_row()])
        page.goto(base_url + "/")
        page.set_input_files("#file-input", files=memory_files(["a.png", "b.png", "c.png"]))
        page.set_input_files("#csv-input", files=pdf_payload())
        wait_for_ingest(playwright_api, page)

        page.click("#scan-button")
        wait_for_banner(playwright_api, page)
        notice = page.locator("#match-notice")
        playwright_api.expect(notice).to_be_visible()
        playwright_api.expect(notice).to_contain_text(
            "1 extra photo scanned without submittal data"
        )
        playwright_api.expect(page.locator("#banner-text")).to_have_text(
            "3 labels scanned — 2 passed, 1 needs review"
        )
        badges = page.eval_on_selector_all(
            "#worksheet-body .status-badge", "els => els.map(e => e.textContent)"
        )
        assert badges.count("PASS") == 2, badges
        assert badges.count("REVIEW") == 1, badges

    def test_qa5_more_form_rows_than_photos_reports_leftover_rows_missing(
        self, page, base_url, form_extractor
    ):
        form_extractor.delegate = FakeFormExtractor(
            rows=[full_row(), full_row(), full_row()]
        )
        page.goto(base_url + "/")
        page.set_input_files("#file-input", files=memory_files(["a.png", "b.png"]))
        page.set_input_files("#csv-input", files=pdf_payload())
        wait_for_ingest(playwright_api, page)

        page.click("#scan-button")
        wait_for_banner(playwright_api, page)
        notice = page.locator("#match-notice")
        playwright_api.expect(notice).to_be_visible()
        playwright_api.expect(notice).to_contain_text(
            "1 form row had no matching photo — listed as MISSING"
        )
        playwright_api.expect(page.locator("#banner-text")).to_have_text(
            "2 labels scanned — 2 passed, 1 form row had no photo"
        )
        badges = page.eval_on_selector_all(
            "#worksheet-body .status-badge", "els => els.map(e => e.textContent)"
        )
        assert badges.count("PASS") == 2, badges
        assert badges.count("MISSING") == 1, badges


class TestQa5MixedNamedUnnamed:
    def test_qa5_ambiguous_leftover_counts_set_unnamed_rows_aside_with_notice(
        self, page, base_url, form_extractor
    ):
        """One row names a photo; two rows are unnamed; three photos remain
        unclaimed -> unnamed(2) != leftover(3) is ambiguous, so the unnamed
        rows become flagged MISSING report rows and the unclaimed photos scan
        WITHOUT submittal data (flagged REVIEW rows), never silently
        mispaired (report-not-block semantics since a9a6260)."""
        form_extractor.delegate = FakeFormExtractor(rows=[
            full_row(filename="b.png"), full_row(), full_row(),
        ])
        page.goto(base_url + "/")
        page.set_input_files(
            "#file-input", files=memory_files(["a.png", "b.png", "c.png", "d.png"])
        )
        page.set_input_files("#csv-input", files=pdf_payload())
        wait_for_ingest(playwright_api, page)

        page.click("#scan-button")
        wait_for_banner(playwright_api, page)
        notice = page.locator("#match-notice")
        playwright_api.expect(notice).to_be_visible()
        playwright_api.expect(notice).to_contain_text(
            "2 form rows had no matching photo — listed as MISSING"
        )
        playwright_api.expect(notice).to_contain_text(
            "3 photos had no form row and were scanned without submittal data"
        )
        playwright_api.expect(worksheet_rows(page)).to_have_count(6)

        # The named photo (b.png) matched -> PASS; the three unclaimed photos
        # have no submittal data -> REVIEW (flagged, never a silent pass);
        # the two unnamed form rows are reported MISSING.
        badges = page.eval_on_selector_all(
            "#worksheet-body .status-badge", "els => els.map(e => e.textContent)"
        )
        assert badges.count("PASS") == 1, f"exactly one matched PASS expected: {badges}"
        assert badges.count("REVIEW") == 3, f"three no-submittal REVIEWs expected: {badges}"
        assert badges.count("MISSING") == 2, f"two MISSING report rows expected: {badges}"

    def test_qa5_unnamed_equals_leftover_pairs_by_order_with_notice(
        self, page, base_url, form_extractor
    ):
        """One named row + one unnamed row, one claimed photo + one leftover
        photo -> unambiguous: the unnamed row pairs to the leftover photo by
        order, both PASS, and the notice reports the by-order match."""
        form_extractor.delegate = FakeFormExtractor(rows=[
            full_row(filename="a.png"), full_row(),
        ])
        page.goto(base_url + "/")
        page.set_input_files("#file-input", files=memory_files(["a.png", "b.png"]))
        page.set_input_files("#csv-input", files=pdf_payload())
        wait_for_ingest(playwright_api, page)

        page.click("#scan-button")
        wait_for_banner(playwright_api, page)
        playwright_api.expect(page.locator("#match-notice")).to_contain_text(
            "Matched 1 row to 1 photo by order"
        )
        badges = page.eval_on_selector_all(
            "#worksheet-body .status-badge", "els => els.map(e => e.textContent)"
        )
        assert badges == ["PASS", "PASS"], badges


class TestQa5DuplicateIngestedNames:
    def test_qa5_case_differing_duplicate_ingested_filenames_block_the_scan(
        self, page, base_url, form_extractor
    ):
        """Two ingested rows name the same photo differing only in case. The
        form-level duplicate warning is surfaced at ingest; at scan the
        serialized manifest collides (server dedup) -> every affected photo is
        an ERROR entry echoing 'more than once'. The scan is not a silent
        mispairing."""
        form_extractor.delegate = FakeFormExtractor(rows=[
            full_row(filename="dup.png", brand="Brand One"),
            full_row(filename="DUP.PNG", brand="Brand Two"),
        ])
        page.goto(base_url + "/")
        page.set_input_files("#file-input", files=memory_files(["dup.png"]))
        page.set_input_files("#csv-input", files=pdf_payload())
        wait_for_ingest(playwright_api, page)

        # The ingest-time duplicate warning is visible before scanning.
        playwright_api.expect(page.locator("#form-warnings")).to_contain_text(
            "same photo more than once"
        )

        page.click("#scan-button")
        wait_for_banner(playwright_api, page)
        row = worksheet_rows(page).first
        playwright_api.expect(row.locator(".status-badge")).to_have_text("ERROR")
        playwright_api.expect(page.locator("#worksheet-body")).to_contain_text(
            "more than once"
        )


class TestQa5SnapshotAtSubmit:
    def test_qa5_removing_the_form_mid_scan_holds_the_ingested_rows_snapshot(
        self, page, base_url, extractor, form_extractor
    ):
        """Extends the gate-4 snapshot finding to the WP7 ingested-rows path:
        the plan snapshots the parsed rows at submit. Removing the form while
        chunk 1 is in flight must NOT make later chunks fall back to the
        placeholder '-' brand — every row keeps the submittal it started with."""
        extractor.delegate = SlowFakeExtractor(GOOD_EXTRACTION, delay=0.4)
        names = [f"l{i:02d}.png" for i in range(12)]  # 2 chunks: 10 + 2
        form_extractor.delegate = FakeFormExtractor(
            rows=[full_row(filename=name) for name in names]
        )
        page.goto(base_url + "/")
        page.set_input_files("#file-input", files=memory_files(names))
        page.set_input_files("#csv-input", files=pdf_payload())
        wait_for_ingest(playwright_api, page)

        page.click("#scan-button")
        # Remove the form while chunk 1 is still running.
        page.click("#csv-clear")
        assert page.locator("#progress-block").is_visible(), (
            "timing guard: the scan finished before the form was removed — the "
            "probe did not exercise the mid-scan path"
        )
        wait_for_banner(playwright_api, page, timeout=30_000)
        playwright_api.expect(worksheet_rows(page)).to_have_count(12)

        # Row 11 arrived in chunk 2 (after removal); its comparison basis must
        # still be the ingested submittal, never the placeholder brand '-'.
        row_11 = worksheet_rows(page).nth(10)
        row_11.locator(".review-button").click()
        panel = page.locator(".detail-panel")
        playwright_api.expect(panel).to_be_visible()
        brand_row = panel.locator(".detail-table tr", has_text="Brand name").first
        submittal_says = (brand_row.locator("td").nth(1).text_content() or "").strip()
        assert submittal_says == "Stone's Throw", (
            f"mid-scan form removal swapped the comparison basis: {submittal_says!r}"
        )
        page.keyboard.press("Escape")

    def test_qa5_reingesting_a_new_form_mid_scan_holds_the_snapshot(
        self, page, base_url, extractor, form_extractor
    ):
        """Swapping in a DIFFERENT form (new PDF -> new ingested rows) mid-scan
        must not change what later chunks are checked against — the snapshot is
        the rows as they were at submit."""
        extractor.delegate = SlowFakeExtractor(GOOD_EXTRACTION, delay=0.4)
        names = [f"m{i:02d}.png" for i in range(12)]
        form_extractor.delegate = FakeFormExtractor(
            rows=[full_row(filename=name) for name in names]
        )
        page.goto(base_url + "/")
        page.set_input_files("#file-input", files=memory_files(names))
        page.set_input_files("#csv-input", files=pdf_payload(name="first.pdf"))
        wait_for_ingest(playwright_api, page)

        page.click("#scan-button")
        # Re-ingest a different form mid-scan: its rows declare a wrong brand.
        form_extractor.delegate = FakeFormExtractor(
            rows=[full_row(filename=name, brand="Wrong Swapped Brand") for name in names]
        )
        page.set_input_files("#csv-input", files=pdf_payload(name="second.pdf"))
        assert page.locator("#progress-block").is_visible(), (
            "timing guard: the scan finished before the form was swapped"
        )
        wait_for_banner(playwright_api, page, timeout=30_000)
        playwright_api.expect(worksheet_rows(page)).to_have_count(12)

        row_11 = worksheet_rows(page).nth(10)
        row_11.locator(".review-button").click()
        panel = page.locator(".detail-panel")
        playwright_api.expect(panel).to_be_visible()
        brand_row = panel.locator(".detail-table tr", has_text="Brand name").first
        submittal_says = (brand_row.locator("td").nth(1).text_content() or "").strip()
        assert submittal_says == "Stone's Throw", (
            f"mid-scan form swap leaked the new form's data into the running "
            f"scan: {submittal_says!r}"
        )
        page.keyboard.press("Escape")
