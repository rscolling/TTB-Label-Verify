"""Capture screenshots of error / problem UI states for docs review."""

from __future__ import annotations

import io
import socket
import threading
import time
from pathlib import Path

import uvicorn
from PIL import Image
from playwright.sync_api import sync_playwright

from app.extraction import BadImageError, ExtractionError
from app.form_ingest import FormRow
from app.main import app, get_extractor, get_form_extractor
from app.models import ExtractedLabel
from app.rules.warning import CANONICAL_WARNING
from tests.conftest import HIGH_CONFIDENCE, FakeExtractor, FakeFormExtractor

OUT = Path(__file__).resolve().parent
OUT.mkdir(parents=True, exist_ok=True)

GOOD = ExtractedLabel(
    brand="STONE'S THROW",
    class_type="Kentucky Straight Bourbon Whiskey",
    alcohol_content="45% Alc./Vol.",
    net_contents="750 mL",
    producer="Blue Ridge Distilling Co., Asheville, NC",
    government_warning=CANONICAL_WARNING,
    warning_prefix_appears_bold=True,
    confidence=dict(HIGH_CONFIDENCE),
    label_detected=True,
)
MISMATCH = ExtractedLabel(
    brand="WRONG BRAND",
    class_type="Kentucky Straight Bourbon Whiskey",
    alcohol_content="40% ABV",
    net_contents="750 mL",
    producer="Blue Ridge Distilling Co., Asheville, NC",
    government_warning=CANONICAL_WARNING,
    warning_prefix_appears_bold=True,
    confidence=dict(HIGH_CONFIDENCE),
    label_detected=True,
)


class Switch:
    def __init__(self) -> None:
        self.delegate: object = FakeExtractor(GOOD)

    def extract(self, image_bytes: bytes) -> ExtractedLabel:
        return self.delegate.extract(image_bytes)  # type: ignore[attr-defined]


class SwitchForm:
    def __init__(self) -> None:
        self.delegate: object = FakeFormExtractor()

    def extract_rows(self, raw: bytes, kind: str):
        return self.delegate.extract_rows(raw, kind)  # type: ignore[attr-defined]


class RoutingExtractor:
    def __init__(self) -> None:
        self.n = 0

    def extract(self, image_bytes: bytes) -> ExtractedLabel:
        self.n += 1
        if self.n == 1:
            return GOOD
        if self.n == 2:
            raise ExtractionError("The label reading service is unavailable right now.")
        return MISMATCH


class BadFileExtractor:
    def extract(self, image_bytes: bytes) -> ExtractedLabel:
        raise BadImageError("not an image")


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), "white").save(buf, format="PNG")
    return buf.getvalue()


PNG = _png()


def files(names: list[str]) -> list[dict]:
    return [{"name": n, "mimeType": "image/png", "buffer": PNG} for n in names]


def csv_payload(text: str, name: str = "submittal.csv") -> list[dict]:
    return [{"name": name, "mimeType": "text/csv", "buffer": text.encode("utf-8")}]


def main() -> None:
    extractor = Switch()
    form_extractor = SwitchForm()
    app.dependency_overrides[get_extractor] = lambda: extractor
    app.dependency_overrides[get_form_extractor] = lambda: form_extractor

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(80):
        if server.started:
            break
        time.sleep(0.05)
    base = f"http://127.0.0.1:{port}"

    manifest = (
        "filename,brand,class_type,abv,net_contents,producer,origin_country,is_import\n"
        "good.png,Stone's Throw,Kentucky Straight Bourbon Whiskey,45%,750 mL,"
        "Blue Ridge Distilling Co. Asheville NC,,false\n"
        "bad.png,Stone's Throw,Kentucky Straight Bourbon Whiskey,45%,750 mL,"
        "Blue Ridge Distilling Co. Asheville NC,,false\n"
        "fail.png,Stone's Throw,Kentucky Straight Bourbon Whiskey,45%,750 mL,"
        "Blue Ridge Distilling Co. Asheville NC,,false\n"
    )

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})

        # 1) Pre-scan validation — Run with no photos
        page.goto(base + "/")
        page.click("#scan-button")
        page.wait_for_selector("#error-callout:not([hidden])")
        page.screenshot(path=str(OUT / "01-no-photos-error.png"), full_page=True)

        # 2) Form/photo count mismatch (blocks scan)
        form_extractor.delegate = FakeFormExtractor(rows=[FormRow(brand="Only One")])
        page.goto(base + "/")
        page.set_input_files("#file-input", files=files(["a.png", "b.png"]))
        page.set_input_files(
            "#csv-input",
            files=[{"name": "form.pdf", "mimeType": "application/pdf", "buffer": b"%PDF-1.4 x"}],
        )
        page.wait_for_timeout(500)
        page.click("#scan-button")
        page.wait_for_selector("#error-callout:not([hidden])")
        page.screenshot(path=str(OUT / "02-form-photo-count-mismatch.png"), full_page=True)

        # 3) Mixed worksheet: PASS + ERROR + FAIL
        extractor.delegate = RoutingExtractor()
        form_extractor.delegate = FakeFormExtractor()
        page.goto(base + "/")
        page.set_input_files("#file-input", files=files(["good.png", "bad.png", "fail.png"]))
        page.set_input_files("#csv-input", files=csv_payload(manifest))
        page.wait_for_timeout(300)
        page.click("#scan-button")
        page.wait_for_selector("#results:not([hidden])")
        page.wait_for_timeout(600)
        page.screenshot(path=str(OUT / "03-worksheet-mixed-pass-error-fail.png"), full_page=True)

        # 4) Error-row drill-down
        err_btn = page.locator("tr.row-error .review-button").first
        if err_btn.count():
            err_btn.click()
            page.wait_for_timeout(300)
            page.screenshot(path=str(OUT / "04-error-row-drilldown.png"), full_page=True)
            page.keyboard.press("Escape")
            page.wait_for_timeout(200)

        # 5) Fail-row drill-down (comparison)
        fail_btn = page.locator("tr.row-fail .review-button").first
        if fail_btn.count():
            fail_btn.click()
            page.wait_for_timeout(300)
            page.screenshot(path=str(OUT / "05-fail-row-drilldown.png"), full_page=True)

        # 6) No submittal → REVIEW (common non-pass state)
        extractor.delegate = FakeExtractor(GOOD)
        page.goto(base + "/")
        page.set_input_files("#file-input", files=files(["solo.png"]))
        page.click("#scan-button")
        page.wait_for_selector("#results:not([hidden])")
        page.wait_for_timeout(500)
        page.screenshot(path=str(OUT / "06-no-submittal-review.png"), full_page=True)

        # 7) All rows bad-file ERROR
        extractor.delegate = BadFileExtractor()
        page.goto(base + "/")
        page.set_input_files("#file-input", files=files(["corrupt.png"]))
        page.set_input_files(
            "#csv-input",
            files=csv_payload("filename,brand\ncorrupt.png,Stone's Throw\n"),
        )
        page.wait_for_timeout(200)
        page.click("#scan-button")
        page.wait_for_selector("#results:not([hidden])")
        page.wait_for_timeout(500)
        page.screenshot(path=str(OUT / "07-all-rows-bad-file.png"), full_page=True)

        browser.close()

    server.should_exit = True
    print("Wrote:")
    for path in sorted(OUT.glob("*.png")):
        print(f"  {path.name} ({path.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
