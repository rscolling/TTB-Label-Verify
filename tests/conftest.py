"""Shared fixtures: image bytes, a FakeExtractor, and an API client factory.

No test in this suite touches the network or needs an API key.
"""

from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.extraction import Extractor
from app.main import app, get_extractor
from app.models import ExtractedLabel
from app.rules.warning import CANONICAL_WARNING

HIGH_CONFIDENCE = {
    "brand": 0.98,
    "class_type": 0.97,
    "alcohol_content": 0.99,
    "net_contents": 0.99,
    "producer": 0.95,
    "origin_country": 0.95,
    "government_warning": 0.96,
}


class FakeExtractor:
    """Test double for the Extractor protocol: canned result or canned error."""

    def __init__(self, result: ExtractedLabel | None = None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls = 0

    def extract(self, image_bytes: bytes) -> ExtractedLabel:
        self.calls += 1
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


@pytest.fixture
def png_bytes() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (64, 64), "white").save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def good_extraction() -> ExtractedLabel:
    """A clean, high-confidence extraction that fully matches `good_application`."""
    return ExtractedLabel(
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


@pytest.fixture
def good_application() -> dict[str, str]:
    """Form data matching `good_extraction` (trap 1: brand differs only in case)."""
    return {
        "brand": "Stone's Throw",
        "class_type": "Kentucky Straight Bourbon Whiskey",
        "abv": "45%",
        "net_contents": "75 cL",
        "producer": "Blue Ridge Distilling Co., 12 Main Street, Asheville, NC 28801",
        "is_import": "false",
    }


@pytest.fixture
def make_client():
    """Build a TestClient with the extractor dependency overridden."""

    def _make(extractor: Extractor) -> TestClient:
        app.dependency_overrides[get_extractor] = lambda: extractor
        return TestClient(app)

    yield _make
    app.dependency_overrides.clear()
