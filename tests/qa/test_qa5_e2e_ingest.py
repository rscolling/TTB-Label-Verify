"""QA gate 5 — ingestion security (XSS) + serialization round-trip integrity.

WP7 makes the LLM form extractor's output attacker-controlled: a malicious
uploaded PDF/photo controls every FormRow string. This module drives the real
browser UI with a hostile FakeFormExtractor and asserts:

  * XSS inertness on every NEW render surface: the "Show what was read"
    preview table, the csv-status line (echoes the form's file name), the
    form-warnings list (echoes row numbers AND hostile photo filenames), the
    pre-scan matching error callouts, the worksheet, and the drill-down's
    "Submittal form says" column. window.__pwned must stay undefined.
  * Round-trip integrity: hostile ingested values (commas, quotes, newlines,
    formula-leading =+-@, emoji, 10k-char strings) survive the client's
    manifest-CSV serialization and the server's manifest parse EXACTLY — the
    drill-down "Submittal form says" cells must equal the ingested values
    byte-for-byte.

Same harness as the earlier QA e2e suites (real FastAPI app on a loopback
port, headless Chromium, both extractors faked). Marked ``e2e``.
"""

from __future__ import annotations

import pytest

playwright_api = pytest.importorskip(
    "playwright.sync_api", reason="playwright is not installed"
)

from app.form_ingest import FormRow
from tests.conftest import FakeExtractor, FakeFormExtractor
from tests.qa._qa_worksheet_harness import (
    GOOD_EXTRACTION,
    HOSTILE_FILENAME,
    XSS_IMG,
    XSS_SCRIPT,
    SwitchableExtractor,
    SwitchableFormExtractor,
    browser_page,
    csv_payload,
    memory_files,
    pdf_payload,
    serve,
    wait_for_banner,
    wait_for_ingest,
    worksheet_rows,
)

pytestmark = pytest.mark.e2e

HOSTILE_FORM_NAME = "<img src=x onerror=window.__pwned=1>.pdf"


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


def assert_not_pwned(page):
    assert page.evaluate("window.__pwned") in (None, False), (
        "an injected payload EXECUTED — window.__pwned was set"
    )
    assert page.evaluate(
        "document.querySelectorAll('img[src=\"x\"], img[src=\"X\"]').length"
    ) == 0, "hostile <img> markup became a live element"


def detail_expected_by_field(page) -> dict[str, str]:
    """{field display name: 'Submittal form says' cell text} for the OPEN panel."""
    return page.evaluate(
        """() => {
             const out = {};
             document.querySelectorAll('.detail-panel .detail-table tr').forEach(tr => {
               const tds = tr.querySelectorAll('td');
               if (tds.length >= 5) { out[tds[0].textContent] = tds[1].textContent; }
             });
             return out;
           }"""
    )


class TestQa5IngestXss:
    def test_qa5_hostile_llm_rows_render_inert_in_preview_status_and_warnings(
        self, page, base_url, form_extractor
    ):
        """A malicious document makes the extractor return markup in every
        field, duplicate hostile filenames (-> the duplicate-photo warning
        echoes them), and a missing brand (-> the fix-the-form warning). All
        of it must render as literal text."""
        form_extractor.delegate = FakeFormExtractor(rows=[
            FormRow(
                filename=HOSTILE_FILENAME,
                brand=XSS_IMG,
                class_type=f"Bourbon {XSS_IMG}",
                abv=f"45% {XSS_SCRIPT}",
                net_contents=f"750 mL {XSS_IMG}",
                producer=f"Distiller {XSS_SCRIPT}",
                origin_country=f"France {XSS_IMG}",
                row_notes=XSS_SCRIPT,
            ),
            # Case-differing duplicate of the hostile filename + no brand:
            # both warning renderers receive attacker text.
            FormRow(filename=HOSTILE_FILENAME.upper(), brand=None),
        ])
        page.goto(base_url + "/")
        page.set_input_files("#csv-input", files=pdf_payload(name=HOSTILE_FORM_NAME))
        wait_for_ingest(playwright_api, page)

        # Status line echoes the hostile FORM file name — as text.
        playwright_api.expect(page.locator("#csv-status")).to_contain_text(
            "Read 2 rows"
        )
        playwright_api.expect(page.locator("#csv-status")).to_contain_text(
            HOSTILE_FORM_NAME
        )

        # Both warnings visible, echoing the hostile photo filename as text.
        warnings = page.locator("#form-warnings li")
        assert warnings.count() >= 2, "expected the duplicate + missing-brand warnings"
        playwright_api.expect(page.locator("#form-warnings")).to_contain_text(
            "more than once"
        )
        playwright_api.expect(page.locator("#form-warnings")).to_contain_text(
            "no brand name"
        )

        # Preview renders every hostile field as literal text.
        preview = page.locator("#ingest-preview")
        playwright_api.expect(preview).to_be_visible()
        preview.locator("summary").click()
        playwright_api.expect(preview).to_contain_text("Bourbon <img src=x")
        playwright_api.expect(preview).to_contain_text("<script>")

        # No markup went live anywhere in the form step.
        step_html = page.eval_on_selector("#csv-dropzone", "el => el.innerHTML")
        assert "<img src=x" not in step_html, "raw <img> markup in the form step"
        assert "<IMG SRC=X" not in step_html, "raw uppercase <img> markup in the form step"
        assert "<script>" not in step_html, "raw <script> markup in the form step"
        assert_not_pwned(page)

    def test_qa5_hostile_expected_values_render_inert_through_scan_and_drilldown(
        self, page, base_url, form_extractor
    ):
        """The hostile INGESTED values ride the serialized manifest into
        /api/verify-batch and come back as `expected` — the worksheet and the
        drill-down comparison column must render them inert."""
        form_extractor.delegate = FakeFormExtractor(rows=[
            FormRow(
                filename=HOSTILE_FILENAME,
                brand=XSS_IMG,
                class_type=f"Bourbon {XSS_IMG}",
                abv=f"45% {XSS_SCRIPT}",
                net_contents=f"750 mL {XSS_IMG}",
                producer=f"Distiller {XSS_SCRIPT}",
                origin_country=f"France {XSS_IMG}",
            ),
        ])
        page.goto(base_url + "/")
        page.set_input_files("#file-input", files=memory_files([HOSTILE_FILENAME]))
        page.set_input_files("#csv-input", files=pdf_payload(name=HOSTILE_FORM_NAME))
        wait_for_ingest(playwright_api, page)

        page.click("#scan-button")
        wait_for_banner(playwright_api, page)
        playwright_api.expect(worksheet_rows(page)).to_have_count(1)

        row = worksheet_rows(page).first
        playwright_api.expect(row.locator(".status-badge")).to_have_text("FAIL")
        row.locator(".review-button").click()
        playwright_api.expect(page.locator(".detail-panel")).to_be_visible()

        expected = detail_expected_by_field(page)
        assert expected["Brand name"] == XSS_IMG, (
            "the ingested hostile brand did not round-trip as literal text"
        )
        assert XSS_SCRIPT in expected["Producer"]

        results_html = page.eval_on_selector("#results", "el => el.innerHTML")
        assert "<img src=x" not in results_html, "raw markup in the worksheet/panel"
        assert "<script>" not in results_html, "raw script tag in the worksheet/panel"
        assert_not_pwned(page)
        page.keyboard.press("Escape")

    def test_qa5_hostile_unrecognized_csv_header_warning_is_inert(self, page, base_url):
        header_attack = "brand,<img src=x onerror=window.__pwned=1>\nStone's Throw,x\n"
        page.goto(base_url + "/")
        page.set_input_files("#csv-input", files=csv_payload(header_attack))
        wait_for_ingest(playwright_api, page)

        playwright_api.expect(page.locator("#form-warnings")).to_be_visible()
        playwright_api.expect(page.locator("#form-warnings")).to_contain_text(
            "Ignored unrecognized column"
        )
        warnings_html = page.eval_on_selector("#form-warnings", "el => el.innerHTML")
        assert "<img src=x" not in warnings_html, "hostile header went live in the warning"
        assert_not_pwned(page)

    def test_qa5_hostile_duplicate_photo_names_error_callout_is_inert(
        self, page, base_url, form_extractor
    ):
        """Order-matching against case-differing duplicate PHOTO names blocks
        the scan; the error callout echoes the hostile photo name as text."""
        form_extractor.delegate = FakeFormExtractor(rows=[
            FormRow(brand="One"), FormRow(brand="Two"),
        ])
        page.goto(base_url + "/")
        page.set_input_files(
            "#file-input",
            files=memory_files([HOSTILE_FILENAME, HOSTILE_FILENAME.upper()]),
        )
        page.set_input_files("#csv-input", files=pdf_payload())
        wait_for_ingest(playwright_api, page)

        page.click("#scan-button")
        callout = page.locator("#error-callout")
        playwright_api.expect(callout).to_be_visible()
        playwright_api.expect(callout).to_contain_text("share the file name")
        playwright_api.expect(page.locator("#results")).to_be_hidden()
        callout_html = page.eval_on_selector("#error-callout", "el => el.innerHTML")
        assert "<img src=x" not in callout_html and "<IMG SRC=X" not in callout_html
        assert_not_pwned(page)


NASTY_ROW = {
    "brand": 'Br"and, «étrange» 🥃',
    "class_type": '=HYPERLINK("http://evil.example/x")',
    "abv": "+45% Alc./Vol.",
    "net_contents": "-750 mL",
    "producer": 'Line1\nLine2, "quoted" Co.',
    "origin_country": "@import url(evil)",
}
GIANT_PRODUCER = "Prefix Distilling Co. " + "x" * 10_000


class TestQa5SerializationRoundTrip:
    def test_qa5_hostile_values_round_trip_exactly_to_the_verification_basis(
        self, page, base_url, extractor, form_extractor
    ):
        """Ingested rows -> client manifest-CSV serialization -> server
        manifest parse -> verify. The application values the server verified
        against (echoed as `expected`, rendered in "Submittal form says") must
        equal the ingested values EXACTLY — no mangling, no truncation, no
        formula-guard mutation, no injection."""
        extractor.delegate = FakeExtractor(GOOD_EXTRACTION)
        form_extractor.delegate = FakeFormExtractor(rows=[
            FormRow(is_import=True, **NASTY_ROW),               # unnamed row 1
            FormRow(brand="Big Cell", producer=GIANT_PRODUCER),  # unnamed row 2
        ])
        page.goto(base_url + "/")
        page.set_input_files("#file-input", files=memory_files(["one.png", "two.png"]))
        page.set_input_files("#csv-input", files=pdf_payload())
        wait_for_ingest(playwright_api, page)

        page.click("#scan-button")
        wait_for_banner(playwright_api, page)
        playwright_api.expect(worksheet_rows(page)).to_have_count(2)
        playwright_api.expect(page.locator("#match-notice")).to_contain_text(
            "Matched 2 rows to 2 photos by order"
        )
        assert extractor.delegate.calls == 2, "each photo must be extracted exactly once"

        # Row 1: punctuation, comma, quotes, emoji, formula-leading, newline.
        worksheet_rows(page).nth(0).locator(".review-button").click()
        playwright_api.expect(page.locator(".detail-panel")).to_be_visible()
        expected = detail_expected_by_field(page)
        assert expected["Brand name"] == NASTY_ROW["brand"]
        assert expected["Kind of drink"] == NASTY_ROW["class_type"], (
            "formula-leading value was mutated in the round trip (the "
            "formula guard belongs to the EXPORT CSV only, never the manifest)"
        )
        assert expected["Alcohol content"] == NASTY_ROW["abv"]
        assert expected["Amount in bottle"] == NASTY_ROW["net_contents"]
        assert expected["Producer"] == NASTY_ROW["producer"], (
            "embedded newline/quotes did not survive the manifest round trip"
        )
        assert expected["Country of origin"] == NASTY_ROW["origin_country"]
        page.keyboard.press("Escape")

        # Row 2: a 10k-char value survives verbatim.
        worksheet_rows(page).nth(1).locator(".review-button").click()
        playwright_api.expect(page.locator(".detail-panel")).to_be_visible()
        expected = detail_expected_by_field(page)
        assert expected["Brand name"] == "Big Cell"
        assert expected["Producer"] == GIANT_PRODUCER, (
            "10k-char producer was truncated or mangled in the round trip"
        )
        page.keyboard.press("Escape")
        assert_not_pwned(page)

    def test_qa5_comma_percent_space_photo_names_survive_order_matching(
        self, page, base_url, form_extractor
    ):
        """Order matching writes the PHOTO's file name into the serialized
        manifest; the file is then re-uploaded by that same name. Commas,
        percents, and spaces in the photo name must round-trip so each photo
        matches its paired row (the double-quote case is a separate FINDING
        below)."""
        tricky_names = ["a,b.png", "50%-off c.png"]
        form_extractor.delegate = FakeFormExtractor(rows=[
            FormRow(brand="First Brand"), FormRow(brand="Second Brand"),
        ])
        page.goto(base_url + "/")
        page.set_input_files("#file-input", files=memory_files(tricky_names))
        page.set_input_files("#csv-input", files=pdf_payload())
        wait_for_ingest(playwright_api, page)

        page.click("#scan-button")
        wait_for_banner(playwright_api, page)
        playwright_api.expect(worksheet_rows(page)).to_have_count(2)

        # No error entries: the serialized manifest parsed, and each photo got
        # its own row (first row -> first photo, per the order-matching rule).
        assert page.locator("#worksheet-body .error-cell").count() == 0, (
            "a comma/percent/space photo name broke the serialized manifest"
        )
        worksheet_rows(page).nth(0).locator(".review-button").click()
        playwright_api.expect(page.locator(".detail-panel")).to_be_visible()
        expected = detail_expected_by_field(page)
        assert expected["Brand name"] == "First Brand"
        page.keyboard.press("Escape")


class TestQa5RoundTripFindings:
    """FINDINGS — round-trip integrity gaps in the WP7 serialize-then-reupload
    path. These assert the CORRECT behavior and are EXPECTED TO FAIL until the
    build agent fixes app/static/app.js (and/or app/batch.py). Per TESTING.md a
    QA finding is either FIXED or documented in APPROACH.md — do not weaken
    them to make the suite green."""

    @pytest.mark.xfail(
        strict=True,
        reason="QA5-F1 (LOW): accepted known limitation, documented in "
        "APPROACH.md. Marker flips to XPASS (a failure) when app.js is fixed — "
        "delete it then.",
    )
    def test_qa5_finding_double_quote_photo_name_breaks_order_matched_manifest(
        self, page, base_url, form_extractor
    ):
        """QA5-F1 (LOW): a photo whose file name contains a double-quote (`"`)
        is order-matched to a form row and the raw name is written into the
        client-serialized manifest CSV verbatim. But the browser percent-
        encodes `"` in the multipart Content-Disposition filename, so the same
        file arrives at /api/verify-batch as e.g. `a%22b.png`. The manifest key
        (`a"b.png`) and the uploaded name (`a%22b.png`) then normalize
        differently, so the photo gets a "no row for this photo" ERROR entry —
        directly contradicting the persistent "Matched 2 rows to 2 photos by
        order" success notice. Isolation: comma, `%`, and space all round-trip
        cleanly; only `"` triggers it (the multipart filename-escaping char).

        A verdict surface must not promise a pairing it then fails to honor.
        Correct behavior: the order-matched photo is verified against its row
        (no error entry), OR the client normalizes the manifest filename the
        same way the wire will (percent-encoding `"`)."""
        form_extractor.delegate = FakeFormExtractor(rows=[
            FormRow(brand="First Brand"), FormRow(brand="Second Brand"),
        ])
        page.goto(base_url + "/")
        page.set_input_files("#file-input", files=memory_files(['a"b.png', "plain.png"]))
        page.set_input_files("#csv-input", files=pdf_payload())
        wait_for_ingest(playwright_api, page)

        page.click("#scan-button")
        wait_for_banner(playwright_api, page)
        playwright_api.expect(worksheet_rows(page)).to_have_count(2)
        # The scan claimed both were matched by order...
        playwright_api.expect(page.locator("#match-notice")).to_contain_text(
            "Matched 2 rows to 2 photos by order"
        )
        # ...so neither may come back as an unmatched "no row for this photo".
        assert page.locator("#worksheet-body .error-cell").count() == 0, (
            "a double-quote photo name broke order-matched serialization: the "
            "photo was NOT verified against its promised row (see QA5-F1)"
        )
