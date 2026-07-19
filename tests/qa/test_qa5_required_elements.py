"""QA gate 5 — required-elements correctness matrix (WP7, client-derived).

Pins the per-class-family required-elements check against a matrix of fake
extractions, driven through the real browser UI:

  * spirits missing ABV -> REVIEW + the 27 CFR 5.61 field-of-vision wording;
  * malt missing ABV -> clean (ABV is conditional for malt, 27 CFR 7.63(a)(3));
  * wine missing ABV -> REVIEW with the generic embossed/other-label reason;
  * unknown/absent class -> generic CORE set only (no ABV requirement);
  * a missing government warning still FAILs (server F7), never merely REVIEW;
  * declared-vs-extracted class-family PRECEDENCE: the code infers the family
    from `field.expected` (the declared/submittal class) and only falls back to
    `field.extracted` when there is no submittal — pin that the DECLARED class
    wins when the two disagree;
  * the required-elements check composes with no-submittal mode (label-intrinsic).

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

FULL = dict(
    net_contents="750 mL",
    producer="Blue Ridge Distilling Co., 12 Main Street, Asheville, NC 28801",
)


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


class TestQa5RequiredElementsMatrix:
    """Each case pins one cell of the required-elements truth table."""

    def _run(self, page, base_url, extractor, form_extractor,
             declared_class, extraction, form_overrides=None):
        extractor.delegate = FakeExtractor(extraction)
        fields = dict(FULL)
        if form_overrides is not None:
            fields.update(form_overrides)
        form_extractor.delegate = FakeFormExtractor(rows=[
            FormRow(brand="Stone's Throw", class_type=declared_class, abv=None, **fields),
        ])
        page.goto(base_url + "/")
        page.set_input_files("#file-input", files=memory_files(["label.png"]))
        page.set_input_files("#csv-input", files=pdf_payload())
        wait_for_ingest(playwright_api, page)
        page.click("#scan-button")
        wait_for_banner(playwright_api, page)
        return worksheet_rows(page).first

    def test_qa5_spirits_missing_abv_review_with_field_of_vision_reason(
        self, page, base_url, extractor, form_extractor
    ):
        row = self._run(
            page, base_url, extractor, form_extractor,
            declared_class="Kentucky Straight Bourbon Whiskey",
            extraction=replace(GOOD_EXTRACTION, alcohol_content=None),
        )
        playwright_api.expect(row.locator(".status-badge")).to_have_text("REVIEW")
        playwright_api.expect(row.locator(".flag")).to_have_count(1)
        playwright_api.expect(
            row.locator('td[data-label="Alcohol content"] .mark')
        ).to_have_attribute("title", "Required element — not found in this photo")
        row.locator(".review-button").click()
        block = page.locator(".required-missing")
        playwright_api.expect(block).to_contain_text(
            "same field of vision as the brand and class (27 CFR 5.61)"
        )
        page.keyboard.press("Escape")

    def test_qa5_malt_missing_abv_is_clean(self, page, base_url, extractor, form_extractor):
        row = self._run(
            page, base_url, extractor, form_extractor,
            declared_class="India Pale Ale",
            extraction=replace(GOOD_EXTRACTION, class_type="India Pale Ale",
                               alcohol_content=None),
        )
        playwright_api.expect(row.locator(".status-badge")).to_have_text("PASS")
        playwright_api.expect(row.locator(".flag")).to_have_count(0)

    def test_qa5_wine_missing_abv_review_with_generic_reason(
        self, page, base_url, extractor, form_extractor
    ):
        row = self._run(
            page, base_url, extractor, form_extractor,
            declared_class="Red Wine",
            extraction=replace(GOOD_EXTRACTION, class_type="Red Wine",
                               alcohol_content=None),
        )
        playwright_api.expect(row.locator(".status-badge")).to_have_text("REVIEW")
        row.locator(".review-button").click()
        block = page.locator(".required-missing")
        playwright_api.expect(block).to_contain_text("Alcohol content")
        playwright_api.expect(block).to_contain_text(
            "may appear on another label or be embossed on the container"
        )
        # Wine must NOT get the spirits-only field-of-vision wording.
        assert "field of vision" not in (block.text_content() or "")
        page.keyboard.press("Escape")

    def test_qa5_unknown_class_uses_generic_core_no_abv_requirement(
        self, page, base_url, extractor, form_extractor
    ):
        # An unclassifiable class/type -> CORE set only; a missing ABV must NOT
        # flag the row (ABV is required for wine/spirits only).
        row = self._run(
            page, base_url, extractor, form_extractor,
            declared_class="Fermented Beverage Specialty",
            extraction=replace(GOOD_EXTRACTION,
                               class_type="Fermented Beverage Specialty",
                               alcohol_content=None),
        )
        playwright_api.expect(row.locator(".status-badge")).to_have_text("PASS")
        playwright_api.expect(row.locator(".flag")).to_have_count(0)

    def test_qa5_unknown_class_still_flags_a_missing_core_element(
        self, page, base_url, extractor, form_extractor
    ):
        # ...but a missing CORE element (net contents) still flags even for an
        # unknown class -> REVIEW. The submittal must ALSO omit net contents
        # (form_overrides), otherwise a declared-but-unextracted value would be
        # a server MISMATCH (FAIL) rather than the required-elements path.
        row = self._run(
            page, base_url, extractor, form_extractor,
            declared_class="Fermented Beverage Specialty",
            extraction=replace(GOOD_EXTRACTION,
                               class_type="Fermented Beverage Specialty",
                               net_contents=None),
            form_overrides={"net_contents": None},
        )
        playwright_api.expect(row.locator(".status-badge")).to_have_text("REVIEW")
        row.locator(".review-button").click()
        playwright_api.expect(page.locator(".required-missing")).to_contain_text(
            "Amount in bottle"
        )
        page.keyboard.press("Escape")

    def test_qa5_missing_warning_fails_not_merely_review(
        self, page, base_url, extractor, form_extractor
    ):
        # The health warning is a required element, but its absence is a server
        # F7 MISMATCH -> FAIL, never softened to REVIEW by the client check.
        row = self._run(
            page, base_url, extractor, form_extractor,
            declared_class="Kentucky Straight Bourbon Whiskey",
            extraction=replace(GOOD_EXTRACTION, government_warning=None),
        )
        playwright_api.expect(row.locator(".status-badge")).to_have_text("FAIL")

    def test_qa5_declared_class_family_wins_over_a_conflicting_extracted_class(
        self, page, base_url, extractor, form_extractor
    ):
        """PRECEDENCE: family is inferred from the DECLARED (submittal) class
        first (field.expected), falling back to the extracted class only when
        there is no submittal. Declare spirits but have the label extract as a
        malt beverage, both with no ABV.

        The disagreeing class values are themselves a class MISMATCH (so the
        row is FAIL regardless), so precedence is pinned via the required-
        elements REASON, not the badge: if the DECLARED (spirits) family wins,
        ABV is required and the drill-down carries the 27 CFR 5.61 field-of-
        vision reason; if the extracted (malt) family had wrongly won, ABV
        would not be required and no ABV element would appear at all."""
        row = self._run(
            page, base_url, extractor, form_extractor,
            declared_class="Kentucky Straight Bourbon Whiskey",  # spirits (declared)
            extraction=replace(GOOD_EXTRACTION, class_type="India Pale Ale",  # malt (extracted)
                               alcohol_content=None),
        )
        # Class mismatch -> FAIL; the required check never downgrades it.
        playwright_api.expect(row.locator(".status-badge")).to_have_text("FAIL")
        row.locator(".review-button").click()
        block = page.locator(".required-missing")
        playwright_api.expect(block).to_contain_text("Alcohol content")
        playwright_api.expect(block).to_contain_text(
            "27 CFR 5.61"  # the spirits-specific reason -> DECLARED family used
        )
        page.keyboard.press("Escape")


class TestQa5RequiredElementsNoSubmittal:
    def test_qa5_required_check_composes_with_no_submittal_mode(
        self, page, base_url, extractor
    ):
        """No form at all: the check is label-intrinsic and infers the family
        from the EXTRACTED class. Spirits with a missing ABV -> REVIEW, flagged
        in the drill-down, even with zero submittal data."""
        extractor.delegate = FakeExtractor(replace(GOOD_EXTRACTION, alcohol_content=None))
        page.goto(base_url + "/")
        page.set_input_files("#file-input", files=memory_files(["label.png"]))
        page.click("#scan-button")
        wait_for_banner(playwright_api, page)
        row = worksheet_rows(page).first
        playwright_api.expect(row.locator(".status-badge")).to_have_text("REVIEW")
        row.locator(".review-button").click()
        block = page.locator(".required-missing")
        playwright_api.expect(block).to_contain_text("Alcohol content")
        # Even with no submittal, spirits family from the extracted class ->
        # the field-of-vision reason (brand + class present, ABV absent).
        playwright_api.expect(block).to_contain_text("27 CFR 5.61")
        page.keyboard.press("Escape")
