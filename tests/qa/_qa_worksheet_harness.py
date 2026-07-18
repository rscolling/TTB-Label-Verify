"""Shared harness for the QA worksheet e2e suites (gates 2-4).

NOT a test module (no test_ prefix). Imported by tests/qa/test_qa2_e2e.py,
tests/qa/test_qa3_e2e_batch.py, and tests/qa/test_qa4_worksheet_probes.py
AFTER each of them has run ``pytest.importorskip("playwright.sync_api")`` —
so this module may import uvicorn freely but must not import playwright at
module level.

Provides: the canned good/hostile extractions, in-memory upload payload
builders, the switchable/slow fake extractors, and generator helpers that the
test modules wrap in their own module-scoped fixtures (one uvicorn server +
one Chromium page per module, mirroring the build agent's harness in
tests/test_e2e_ui.py).
"""

from __future__ import annotations

import io
import socket
import threading
import time

import pytest
import uvicorn
from PIL import Image

from app.main import app, get_extractor
from app.models import ExtractedLabel
from app.rules.warning import CANONICAL_WARNING
from tests.conftest import HIGH_CONFIDENCE, FakeExtractor

# Two distinct XSS payloads: an onerror image (fires on HTML parse) and a
# script tag. Either executing sets window.__pwned. If the UI is safe, both
# remain literal text everywhere they are rendered.
XSS_IMG = '<img src=x onerror="window.__pwned=1">'
XSS_SCRIPT = "<script>window.__pwned=1</script>"
# Quote-free variant usable as a FILE NAME and inside naive CSV cells.
HOSTILE_FILENAME = "<img src=x onerror=window.__pwned=1>.png"

MANIFEST_HEADER = (
    "filename,brand,class_type,abv,net_contents,producer,origin_country,is_import"
)

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

# Hostile markup in EVERY string field; the warning carries the script payload
# inside clause (2) so the clause-diff renderer receives it in `found`.
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


def _tiny_png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (2, 2), "white").save(buffer, format="PNG")
    return buffer.getvalue()


PNG = _tiny_png()


def memory_files(names: list[str]) -> list[dict]:
    """In-memory upload payloads — file NAMES may carry characters Windows
    forbids on disk (<, >, ")."""
    return [{"name": name, "mimeType": "image/png", "buffer": PNG} for name in names]


def csv_payload(text: str, name: str = "submittal.csv") -> list[dict]:
    return [{"name": name, "mimeType": "text/csv", "buffer": text.encode("utf-8")}]


class SwitchableExtractor:
    """One extractor instance whose behavior each test can reconfigure."""

    def __init__(self) -> None:
        self.delegate = FakeExtractor(GOOD_EXTRACTION)

    def extract(self, image_bytes: bytes) -> ExtractedLabel:
        return self.delegate.extract(image_bytes)


class SlowFakeExtractor(FakeExtractor):
    """Adds per-label latency so in-flight states are observable."""

    def __init__(self, result: ExtractedLabel, delay: float) -> None:
        super().__init__(result)
        self.delay = delay

    def extract(self, image_bytes: bytes) -> ExtractedLabel:
        time.sleep(self.delay)
        return super().extract(image_bytes)


def serve(extractor) -> "typing.Iterator[str]":  # noqa: F821 - doc only
    """Generator for a module-scoped base_url fixture: real FastAPI app on a
    free loopback port in a background uvicorn thread, extractor overridden."""
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


def browser_page(playwright_api) -> "typing.Iterator":  # noqa: F821 - doc only
    """Generator for a module-scoped Playwright page fixture; skips cleanly
    when Chromium is unavailable."""
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


def worksheet_rows(page):
    return page.locator("#worksheet-body > tr.worksheet-row")


def wait_for_banner(playwright_api, page, timeout: int = 15_000):
    playwright_api.expect(page.locator("#banner-text")).to_contain_text(
        "scanned", timeout=timeout
    )


def download_via(page, selector: str, tmp_path, filename: str) -> bytes:
    """Click a download control and return the artifact's bytes."""
    with page.expect_download() as download_info:
        page.click(selector)
    path = tmp_path / filename
    download_info.value.save_as(path)
    return path.read_bytes()
