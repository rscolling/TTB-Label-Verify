"""Label field extraction — AI for perception, nothing else.

`Extractor` is the seam the API and tests depend on; `ClaudeExtractor` is the
production implementation (single Claude vision call, structured output via a
forced tool-use schema). The Anthropic client is created lazily so the module
imports and constructs cleanly without an API key.
"""

from __future__ import annotations

import base64
import io
import os
from typing import Any, Protocol

from PIL import Image, UnidentifiedImageError

from app.models import ExtractedLabel

DEFAULT_MODEL = "claude-sonnet-5"
MAX_DIMENSION = 1568  # Anthropic vision sweet spot; also caps upload size (R2).
JPEG_QUALITY = 85


class BadImageError(Exception):
    """The uploaded bytes are not a decodable image."""


class ExtractionError(Exception):
    """The extraction backend failed (API error, timeout, malformed output)."""


class Extractor(Protocol):
    """Anything that can read the 7 required fields off a label image."""

    def extract(self, image_bytes: bytes) -> ExtractedLabel:  # pragma: no cover - protocol
        ...


def prepare_image(image_bytes: bytes) -> tuple[bytes, str]:
    """Validate and downscale an image for upload; never reject an oversized one.

    Returns (jpeg_bytes, media_type). Raises BadImageError for non-image input.
    """
    try:
        image = Image.open(io.BytesIO(image_bytes))
        image.load()
    except (UnidentifiedImageError, OSError) as exc:
        raise BadImageError("The uploaded file is not a readable image.") from exc

    if max(image.size) > MAX_DIMENSION:
        image.thumbnail((MAX_DIMENSION, MAX_DIMENSION), Image.LANCZOS)
    if image.mode != "RGB":
        image = image.convert("RGB")
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=JPEG_QUALITY)
    return buffer.getvalue(), "image/jpeg"


_FIELD_SCHEMA: dict[str, Any] = {"type": ["string", "null"]}
_CONFIDENCE_SCHEMA: dict[str, Any] = {"type": "number", "minimum": 0, "maximum": 1}

_EXTRACTION_TOOL: dict[str, Any] = {
    "name": "record_label_fields",
    "description": "Record the fields transcribed from an alcohol beverage label image.",
    "input_schema": {
        "type": "object",
        "properties": {
            "label_detected": {
                "type": "boolean",
                "description": "False if the image does not contain a readable alcohol beverage label.",
            },
            "brand": {
                **_FIELD_SCHEMA,
                "description": (
                    "The primary brand name only, exactly as printed — exclude taglines, "
                    "series names, or decorative text above or below it."
                ),
            },
            "class_type": {
                **_FIELD_SCHEMA,
                "description": "Class/type designation (e.g. 'Kentucky Straight Bourbon Whiskey').",
            },
            "alcohol_content": {
                **_FIELD_SCHEMA,
                "description": "Alcohol content exactly as printed (e.g. '45% Alc./Vol.', '90 Proof').",
            },
            "net_contents": {**_FIELD_SCHEMA, "description": "Net contents exactly as printed (e.g. '750 mL')."},
            "producer": {
                **_FIELD_SCHEMA,
                "description": "Producer/bottler name and address as printed, comma-separated.",
            },
            "origin_country": {
                **_FIELD_SCHEMA,
                "description": "Country of origin statement if present (e.g. 'Product of France').",
            },
            "government_warning": {
                **_FIELD_SCHEMA,
                "description": (
                    "The full government health warning, transcribed verbatim with the "
                    "original capitalization preserved exactly."
                ),
            },
            "warning_prefix_appears_bold": {
                "type": ["boolean", "null"],
                "description": "Best-effort: does the 'GOVERNMENT WARNING:' prefix appear bolder than the body text?",
            },
            "confidence": {
                "type": "object",
                "description": "Per-field transcription confidence, 0 to 1.",
                "properties": {
                    field: _CONFIDENCE_SCHEMA
                    for field in (
                        "brand",
                        "class_type",
                        "alcohol_content",
                        "net_contents",
                        "producer",
                        "origin_country",
                        "government_warning",
                    )
                },
            },
        },
        "required": ["label_detected", "confidence"],
    },
}

_SYSTEM_PROMPT = (
    "You transcribe alcohol beverage label images for a compliance workflow. "
    "Transcribe exactly what is printed — never correct, complete, or normalize "
    "the text; capitalization must be preserved verbatim (especially for the "
    "government warning). Transcribe the government warning with particular "
    "care, word by word — re-read your transcription against the image before "
    "answering. Use null for fields that are not visible, and lower the "
    "confidence for anything blurry, glared, or at a steep angle."
)


class ClaudeExtractor:
    """Single Claude vision call with a forced structured-output tool."""

    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        self._api_key = api_key
        # Resolved at construction (after load_dotenv), not import: the
        # EXTRACTION_MODEL env var is the speed/accuracy knob (see APPROACH.md).
        self._model = model or os.environ.get("EXTRACTION_MODEL", DEFAULT_MODEL)
        self._client: Any = None  # lazy: constructing must not require a key

    def _get_client(self) -> Any:
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic(api_key=self._api_key or os.environ.get("ANTHROPIC_API_KEY"))
        return self._client

    def extract(self, image_bytes: bytes) -> ExtractedLabel:
        """Extract all fields in one vision call. Retries once on API failure."""
        prepared, media_type = prepare_image(image_bytes)
        import anthropic

        last_error: Exception | None = None
        for _ in range(2):  # one retry per SPEC.md error handling
            try:
                response = self._get_client().messages.create(
                    model=self._model,
                    max_tokens=1500,
                    system=_SYSTEM_PROMPT,
                    tools=[_EXTRACTION_TOOL],
                    tool_choice={"type": "tool", "name": "record_label_fields"},
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "image",
                                    "source": {
                                        "type": "base64",
                                        "media_type": media_type,
                                        "data": base64.b64encode(prepared).decode("ascii"),
                                    },
                                },
                                {"type": "text", "text": "Transcribe this label's fields."},
                            ],
                        }
                    ],
                )
                return self._parse_response(response)
            except (anthropic.APIConnectionError, TimeoutError) as exc:
                last_error = exc  # transient: retry once
            except ExtractionError as exc:
                last_error = exc  # malformed tool payload: a fresh call usually parses
            except anthropic.APIStatusError as exc:
                if exc.status_code >= 500 or exc.status_code == 429:
                    last_error = exc  # server-side/rate-limit: retry once
                else:
                    # 4xx is permanent — retrying a bad request only doubles the failure.
                    raise ExtractionError("The label reading service rejected the request.") from exc
        raise ExtractionError("The label reading service is unavailable right now.") from last_error

    @staticmethod
    def _parse_response(response: Any) -> ExtractedLabel:
        tool_use = next((block for block in response.content if block.type == "tool_use"), None)
        if tool_use is None or not isinstance(tool_use.input, dict):
            raise ExtractionError("The label reading service returned an unexpected response.")
        data: dict[str, Any] = tool_use.input
        try:
            return ExtractedLabel(
                brand=data.get("brand"),
                class_type=data.get("class_type"),
                alcohol_content=data.get("alcohol_content"),
                net_contents=data.get("net_contents"),
                producer=data.get("producer"),
                origin_country=data.get("origin_country"),
                government_warning=data.get("government_warning"),
                warning_prefix_appears_bold=data.get("warning_prefix_appears_bold"),
                confidence={k: float(v) for k, v in (data.get("confidence") or {}).items()},
                label_detected=bool(data.get("label_detected", True)),
            )
        except (TypeError, ValueError) as exc:
            raise ExtractionError("The label reading service returned an unexpected response.") from exc
