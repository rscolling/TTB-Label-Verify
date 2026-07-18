"""QA gate 1 — OPEN FINDINGS (these tests assert SPEC-correct behavior and are
EXPECTED TO FAIL until the build agent fixes the underlying bugs).

Each test name is cited as evidence in the QA findings report. Do not delete or
weaken these tests to make the suite green — fix the code (TESTING.md: a QA
finding is either FIXED or documented in APPROACH.md as a known limitation).

Findings covered here:
  QA-F1  ZeroDivisionError when the application's net_contents parses to 0
         ("0 mL" is a valid form input) — user-controlled 500 crash.
  QA-F2  ClaudeExtractor._parse_response does not validate the tool payload:
         a non-numeric confidence value raises ValueError instead of
         ExtractionError, escaping the retry/502 handling as a raw 500.
  QA-F3  Non-string field values in the tool payload (schema violation by the
         model) pass through ExtractedLabel and crash the rules engine
         (AttributeError) — surfaces as a raw 500 mid-request.
  QA-F4  An unexpected (non-ExtractionError) exception from the extractor
         returns FastAPI's default 500 body, not the documented friendly
         {"error": {code, message}} shape (SPEC error-handling criterion).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.extraction import ClaudeExtractor, ExtractionError
from app.main import app, get_extractor
from app.models import Verdict
from app.rules.net_contents import match_net_contents
from tests.conftest import FakeExtractor


def _client_no_raise(extractor) -> TestClient:
    app.dependency_overrides[get_extractor] = lambda: extractor
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.clear()


class TestQAF1ZeroNetContents:
    def test_zero_application_net_contents_must_not_crash(self):
        """QA-F1: '0 mL' from the application form must yield a verdict (review),
        never a ZeroDivisionError."""
        result = match_net_contents("750 mL", "0 mL")
        assert result.verdict in (Verdict.REVIEW, Verdict.MISMATCH)

    def test_zero_vs_zero_must_not_crash(self):
        result = match_net_contents("0 mL", "0 mL")
        assert result.verdict in (Verdict.MATCH, Verdict.REVIEW)

    def test_zero_net_contents_via_api_is_not_a_500(
        self, png_bytes, good_extraction, good_application
    ):
        """QA-F1 at L2: a plain form input crashes the endpoint with a 500."""
        good_application["net_contents"] = "0 mL"
        client = _client_no_raise(FakeExtractor(good_extraction))
        response = client.post(
            "/api/verify",
            files={"file": ("label.png", png_bytes, "image/png")},
            data=good_application,
        )
        assert response.status_code == 200, "user-controlled form value must never 500"


class TestQAF2F3PayloadValidation:
    def test_non_numeric_confidence_raises_extraction_error_not_valueerror(self):
        """QA-F2: malformed tool payload must surface as ExtractionError (502 path)."""
        response = SimpleNamespace(
            content=[
                SimpleNamespace(
                    type="tool_use",
                    input={"label_detected": True, "confidence": {"brand": "high"}},
                )
            ]
        )
        with pytest.raises(ExtractionError):
            ClaudeExtractor._parse_response(response)

    def test_non_string_field_value_does_not_crash_the_engine(
        self, png_bytes, good_extraction, good_application
    ):
        """QA-F3: a numeric brand in the tool payload reaches match_brand and
        raises AttributeError ('int' has no .strip) -> raw 500 mid-request."""
        good_extraction.brand = 45  # type: ignore[assignment] — simulates schema-violating payload
        client = _client_no_raise(FakeExtractor(good_extraction))
        response = client.post(
            "/api/verify",
            files={"file": ("label.png", png_bytes, "image/png")},
            data=good_application,
        )
        assert response.status_code != 500, "schema-violating extraction must not 500"


class TestQAF4UnexpectedExtractorException:
    def test_unexpected_exception_returns_friendly_error_shape(
        self, png_bytes, good_application
    ):
        """QA-F4: even an unforeseen bug must render the documented
        {"error": {code, message}} shape, not FastAPI's default 500 body."""
        client = _client_no_raise(FakeExtractor(error=RuntimeError("boom")))
        response = client.post(
            "/api/verify",
            files={"file": ("label.png", png_bytes, "image/png")},
            data=good_application,
        )
        body = response.json()
        assert "error" in body and "message" in body.get("error", {}), (
            "SPEC error-handling: every failure needs a clear friendly message"
        )
