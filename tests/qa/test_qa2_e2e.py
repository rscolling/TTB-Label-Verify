"""QA gate 2 — adversarial browser E2E of the single-label UI (WP2).

Independent of the build agent's tests/test_e2e_ui.py. Boots the real FastAPI
app on a loopback port with the extractor overridden to a switchable fake, and
drives headless Chromium via Playwright. Marked ``e2e``; skips cleanly (never
fails) when Playwright/Chromium is unavailable.

Focus areas:
  * SECURITY — extraction output is UNTRUSTED. Prove by ATTACK that hostile
    markup in every rendered surface (label values, reason prose, clause diff,
    the value-truncation title path, and the ExtractionError message that
    reaches the error callout) renders as INERT TEXT and never executes.
  * ADVERSARIAL UX — double-fire submit, stale-result clearing, import-checkbox
    state, and the na ("Not checked") rendering.
"""

from __future__ import annotations

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

from app.extraction import ExtractionError
from app.main import app, get_extractor
from app.models import ExtractedLabel
from app.rules.warning import CANONICAL_WARNING
from tests.conftest import HIGH_CONFIDENCE, FakeExtractor

pytestmark = pytest.mark.e2e

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SAMPLE_LABEL = REPO_ROOT / "eval" / "labels" / "01-bourbon-clean.png"

# Two distinct XSS payloads: an onerror image (fires on HTML parse) and a script
# tag. Either executing would set window.__pwned. If the UI is safe, both remain
# literal text.
XSS_IMG = '<img src=x onerror="window.__pwned=1">'
XSS_SCRIPT = "<script>window.__pwned=1</script>"

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

# Hostile extraction: markup in EVERY string field. The warning carries the
# script payload inside clause (2) so the clause-diff renderer receives it in
# `found`; the reason prose also echoes differing words.
HOSTILE_WARNING = CANONICAL_WARNING.replace(
    "may cause health problems", f"may cause {XSS_SCRIPT}"
)
HOSTILE_EXTRACTION = ExtractedLabel(
    brand=XSS_IMG,
    class_type=f"Bourbon {XSS_IMG}",
    alcohol_content=f"45% {XSS_IMG}",
    net_contents=f"750 mL {XSS_IMG}",
    producer=f"Distiller {XSS_SCRIPT}",
    origin_country=f"France {XSS_IMG}",
    government_warning=HOSTILE_WARNING,
    warning_prefix_appears_bold=True,
    confidence=dict(HIGH_CONFIDENCE),
    label_detected=True,
)

# A >140-char value carrying a payload, to exercise the truncation + title path.
LONG_HOSTILE = XSS_IMG + " " + ("A" * 200)


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


def submit_label(page, base_url, form, is_import=False):
    page.goto(base_url + "/")
    for field_id, value in form.items():
        page.fill(f"#{field_id}", value)
    if is_import:
        page.check("#is_import")
    page.set_input_files("#file-input", str(SAMPLE_LABEL))
    page.click("#check-button")


class TestXSSInEveryRenderedSurface:
    """The build agent CLAIMS all dynamic text uses textContent. Verify by attack."""

    def test_hostile_extraction_renders_inert_never_executes(
        self, page, base_url, extractor
    ):
        extractor.delegate = FakeExtractor(HOSTILE_EXTRACTION)
        # Provide every field so all matchers run and every value column renders.
        submit_label(
            page,
            base_url,
            {
                "brand": "Legit Brand",
                "class_type": "Bourbon",
                "abv": "45%",
                "net_contents": "750 mL",
                "producer": "Distiller Co.",
            },
            is_import=True,
        )
        page.fill("#origin_country", "France")  # revealed by import; refilled harmlessly
        playwright_api.expect(page.locator("#results")).to_be_visible(timeout=10_000)

        # 1) No payload executed anywhere.
        assert page.evaluate("window.__pwned") in (None, False), "XSS EXECUTED — pwned"

        # 2) The label column shows the markup as LITERAL TEXT (escaped, not parsed).
        body = page.locator("#results-body")
        playwright_api.expect(body).to_contain_text("<img src=x onerror=")
        inner = page.eval_on_selector("#results-body", "el => el.innerHTML")
        assert "<img src=x onerror=" not in inner, "raw <img> injected into DOM"
        assert "&lt;img" in inner, "markup was not HTML-escaped in the DOM"

        # 3) No attacker element actually materialized in the results subtree.
        injected = page.evaluate(
            "document.querySelectorAll('#results img[src=\"x\"], #results script').length"
        )
        assert injected == 0, "attacker <img>/<script> element was created"

        # 4) The clause-diff surface received hostile `found` text — still inert.
        assert page.evaluate("window.__pwned") in (None, False)
        diff_html = page.eval_on_selector("#results", "el => el.innerHTML")
        assert "<script>window.__pwned" not in diff_html, "raw <script> in clause diff"

    def test_truncation_title_path_is_inert(self, page, base_url, extractor):
        hostile = replace(GOOD_EXTRACTION, producer=LONG_HOSTILE)
        extractor.delegate = FakeExtractor(hostile)
        submit_label(
            page, base_url, {"brand": "Legit Brand", "producer": "Distiller"}
        )
        playwright_api.expect(page.locator("#results")).to_be_visible(timeout=10_000)
        assert page.evaluate("window.__pwned") in (None, False)
        # The long value is truncated with a title attribute holding the full text
        # as an ATTRIBUTE STRING (never parsed as HTML).
        title = page.evaluate(
            "Array.from(document.querySelectorAll('#results-body td.value-cell'))"
            ".map(td => td.getAttribute('title')).find(t => t && t.indexOf('onerror') !== -1)"
        )
        assert title is not None and "<img" in title, "title should hold literal markup"
        assert page.evaluate("window.__pwned") in (None, False)

    def test_hostile_error_message_renders_inert_in_callout(
        self, page, base_url, extractor
    ):
        # A hostile ExtractionError message flows: server error.message -> JS
        # Error -> showError -> errorMessage. Prove the callout can't execute it.
        extractor.delegate = FakeExtractor(
            error=ExtractionError(f"Reader down {XSS_IMG}")
        )
        submit_label(page, base_url, {"brand": "Legit Brand"})
        callout = page.locator("#error-callout")
        playwright_api.expect(callout).to_be_visible(timeout=10_000)
        assert page.evaluate("window.__pwned") in (None, False)
        msg_html = page.eval_on_selector("#error-message", "el => el.innerHTML")
        assert "<img src=x onerror=" not in msg_html, "raw markup in error callout"
        playwright_api.expect(page.locator("#error-message")).to_contain_text("<img")

    def test_image_preview_uses_object_url_not_data_uri(self, page, base_url):
        submit_label(page, base_url, {"brand": "Legit Brand"})
        playwright_api.expect(page.locator("#results")).to_be_visible(timeout=10_000)
        src = page.get_attribute("#result-image", "src")
        assert src and src.startswith("blob:"), f"preview src should be a blob: URL, got {src!r}"


class TestAdversarialSubmit:
    def test_rapid_double_click_fires_at_most_one_request(self, page, base_url):
        page.goto(base_url + "/")
        page.fill("#brand", "Legit Brand")
        page.set_input_files("#file-input", str(SAMPLE_LABEL))
        calls = {"n": 0}
        page.on(
            "request",
            lambda req: calls.__setitem__("n", calls["n"] + 1)
            if req.url.endswith("/api/verify")
            else None,
        )
        page.dblclick("#check-button")
        playwright_api.expect(page.locator("#results")).to_be_visible(timeout=10_000)
        time.sleep(0.3)  # let any stray second request land
        assert calls["n"] == 1, f"submit double-fired: {calls['n']} POSTs to /api/verify"

    def test_na_fields_render_not_checked_not_blank(self, page, base_url):
        submit_label(page, base_url, {"brand": "Legit Brand"})
        playwright_api.expect(page.locator("#results")).to_be_visible(timeout=10_000)
        # Every field the application left blank must read "Not checked", never empty.
        na_cells = page.locator("#results-body td.verdict-na")
        count = na_cells.count()
        assert count >= 1
        for i in range(count):
            playwright_api.expect(na_cells.nth(i)).to_contain_text("Not checked")

    def test_new_photo_after_results_does_not_leave_stale_verdicts_visible(
        self, page, base_url, extractor
    ):
        # First run: a clean pass (brand matches the fake extraction).
        submit_label(page, base_url, {"brand": "Stone's Throw"})
        playwright_api.expect(page.locator("#banner-text")).to_have_text(
            "Everything matches", timeout=10_000
        )
        # Now choose a different photo WITHOUT re-checking. Does the old verdict
        # panel stay on screen next to the new photo? (Potential UX confusion.)
        # NB: this must be a genuinely different file — Chromium fires no
        # change event when set_input_files re-sets the identical FileList.
        page.set_input_files("#file-input", str(SAMPLE_LABEL.with_name("02-wine-clean.png")))
        results_still_visible = page.locator("#results").is_visible()
        # QA finding #2 FIXED: selecting a new photo now clears the previous
        # verdict panel, so a stale result can never sit next to a new photo.
        assert results_still_visible is False


class TestImportCheckboxState:
    def test_uncheck_import_sends_domestic_and_country_field_hidden(
        self, page, base_url, extractor
    ):
        page.goto(base_url + "/")
        page.fill("#brand", "Legit Brand")
        page.check("#is_import")
        playwright_api.expect(page.locator("#origin-field")).to_be_visible()
        page.fill("#origin_country", "France")
        page.uncheck("#is_import")
        playwright_api.expect(page.locator("#origin-field")).to_be_hidden()
        # The typed value is retained in the DOM (not destroyed) ...
        assert page.input_value("#origin_country") == "France"
        page.set_input_files("#file-input", str(SAMPLE_LABEL))
        page.click("#check-button")
        playwright_api.expect(page.locator("#results")).to_be_visible(timeout=10_000)
        # ... but because import is unchecked, origin verifies as domestic N/A,
        # NOT as a stale France comparison.
        origin_row = page.locator("#results-body tr", has_text="Country of origin")
        playwright_api.expect(origin_row).to_contain_text("Not checked")
