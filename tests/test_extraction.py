"""L1/L2: extraction module — image preparation and the ClaudeExtractor seam.

The Anthropic client is faked at the `_client` seam; nothing here needs a
network connection or an ANTHROPIC_API_KEY.
"""

from __future__ import annotations

import base64
import io
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx
import pytest
from PIL import Image

from app.extraction import (
    MAX_DIMENSION,
    DEFAULT_MODEL,
    BadImageError,
    ClaudeExtractor,
    ExtractionError,
    prepare_image,
)


def image_bytes(width: int, height: int, mode: str = "RGB", fmt: str = "PNG") -> bytes:
    buffer = io.BytesIO()
    Image.new(mode, (width, height), "white").save(buffer, format=fmt)
    return buffer.getvalue()


def tool_use_response(input_data: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(content=[SimpleNamespace(type="tool_use", input=input_data)])


def connection_error() -> anthropic.APIConnectionError:
    return anthropic.APIConnectionError(
        request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    )


class FakeMessages:
    """Stands in for client.messages: canned responses/errors, captured kwargs."""

    def __init__(self, results: list[Any]) -> None:
        self.results = list(results)  # exceptions are raised, anything else returned
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def make_extractor(results: list[Any]) -> tuple[ClaudeExtractor, FakeMessages]:
    extractor = ClaudeExtractor()
    messages = FakeMessages(results)
    extractor._client = SimpleNamespace(messages=messages)  # inject at the lazy seam
    return extractor, messages


MINIMAL_INPUT = {"label_detected": True, "confidence": {}}


class TestPrepareImage:
    def test_large_image_downscaled_to_max_dimension(self):
        prepared, media_type = prepare_image(image_bytes(3200, 1400))
        result = Image.open(io.BytesIO(prepared))
        assert media_type == "image/jpeg"
        assert max(result.size) == MAX_DIMENSION
        assert result.size[0] / result.size[1] == pytest.approx(3200 / 1400, rel=0.01)

    def test_small_image_keeps_dimensions(self):
        prepared, _ = prepare_image(image_bytes(64, 48))
        assert Image.open(io.BytesIO(prepared)).size == (64, 48)

    def test_rgba_converted_for_jpeg_output(self):
        prepared, _ = prepare_image(image_bytes(32, 32, mode="RGBA"))
        assert Image.open(io.BytesIO(prepared)).mode == "RGB"

    def test_non_image_bytes_raise_bad_image_error(self):
        with pytest.raises(BadImageError):
            prepare_image(b"this is not an image")

    def test_truncated_image_raises_bad_image_error(self):
        with pytest.raises(BadImageError):
            prepare_image(image_bytes(400, 400)[:120])


class TestClaudeExtractorLazyInit:
    def test_constructs_without_api_key(self, monkeypatch):
        """Importing and constructing must never require a key (no key on CI)."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        extractor = ClaudeExtractor()
        assert extractor._client is None  # client is only built on first use

    def test_model_defaults_to_spec_pin(self):
        assert DEFAULT_MODEL == "claude-sonnet-5"


class TestClaudeExtractorExtract:
    def test_single_vision_call_with_forced_tool(self):
        extractor, messages = make_extractor([tool_use_response(MINIMAL_INPUT)])
        extractor.extract(image_bytes(64, 64))

        assert len(messages.calls) == 1  # R2: exactly one vision call
        call = messages.calls[0]
        assert call["model"] == DEFAULT_MODEL
        assert call["tool_choice"] == {"type": "tool", "name": "record_label_fields"}
        assert call["tools"][0]["name"] == "record_label_fields"

    def test_image_downscaled_before_upload(self):
        extractor, messages = make_extractor([tool_use_response(MINIMAL_INPUT)])
        extractor.extract(image_bytes(4000, 3000))

        source = messages.calls[0]["messages"][0]["content"][0]["source"]
        assert source["media_type"] == "image/jpeg"
        sent = Image.open(io.BytesIO(base64.b64decode(source["data"])))
        assert max(sent.size) <= MAX_DIMENSION

    def test_response_fields_mapped_onto_extracted_label(self):
        extractor, _ = make_extractor(
            [
                tool_use_response(
                    {
                        "label_detected": True,
                        "brand": "STONE'S THROW",
                        "alcohol_content": "90 Proof",
                        "warning_prefix_appears_bold": True,
                        "confidence": {"brand": 0.97},
                    }
                )
            ]
        )
        extracted = extractor.extract(image_bytes(64, 64))
        assert extracted.brand == "STONE'S THROW"
        assert extracted.alcohol_content == "90 Proof"
        assert extracted.warning_prefix_appears_bold is True
        assert extracted.confidence == {"brand": 0.97}
        assert extracted.label_detected is True

    def test_label_not_detected_passthrough(self):
        extractor, _ = make_extractor(
            [tool_use_response({"label_detected": False, "confidence": {}})]
        )
        assert extractor.extract(image_bytes(64, 64)).label_detected is False

    def test_bad_image_raises_before_any_api_call(self):
        extractor, messages = make_extractor([])
        with pytest.raises(BadImageError):
            extractor.extract(b"not an image")
        assert messages.calls == []

    def test_transient_api_failure_retried_once_then_succeeds(self):
        extractor, messages = make_extractor(
            [connection_error(), tool_use_response(MINIMAL_INPUT)]
        )
        extracted = extractor.extract(image_bytes(64, 64))
        assert extracted.label_detected is True
        assert len(messages.calls) == 2

    def test_persistent_api_failure_raises_extraction_error_after_one_retry(self):
        extractor, messages = make_extractor([connection_error(), connection_error()])
        with pytest.raises(ExtractionError, match="unavailable"):
            extractor.extract(image_bytes(64, 64))
        assert len(messages.calls) == 2  # SPEC.md: retry once, then error out

    def test_response_without_tool_use_block_is_extraction_error(self):
        bad = SimpleNamespace(content=[SimpleNamespace(type="text", text="hello")])
        extractor, messages = make_extractor([bad, bad])
        with pytest.raises(ExtractionError, match="unexpected response"):
            extractor.extract(image_bytes(64, 64))
        assert len(messages.calls) == 2  # malformed payloads get one fresh retry

    def test_malformed_payload_recovers_on_retry(self):
        bad = SimpleNamespace(content=[SimpleNamespace(type="text", text="hello")])
        extractor, messages = make_extractor([bad, tool_use_response(MINIMAL_INPUT)])
        extracted = extractor.extract(image_bytes(64, 64))
        assert extracted.label_detected is True
        assert len(messages.calls) == 2


class TestModelKnob:
    def test_extraction_model_env_var_overrides_default(self, monkeypatch):
        monkeypatch.setenv("EXTRACTION_MODEL", "claude-haiku-4-5-20251001")
        assert ClaudeExtractor()._model == "claude-haiku-4-5-20251001"

    def test_explicit_model_argument_wins_over_env(self, monkeypatch):
        monkeypatch.setenv("EXTRACTION_MODEL", "claude-haiku-4-5-20251001")
        assert ClaudeExtractor(model="claude-sonnet-5")._model == "claude-sonnet-5"
