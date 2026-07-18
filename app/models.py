"""Shared data structures for extraction and verification.

Plain dataclasses (no framework coupling): the rules engine stays importable
and testable without FastAPI, and FastAPI serializes dataclasses natively.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Verdict(str, Enum):
    """Per-field verification outcome."""

    MATCH = "match"
    REVIEW = "review"
    MISMATCH = "mismatch"
    NA = "na"


@dataclass
class FieldResult:
    """Structured result of one field matcher.

    Attributes:
        field: canonical field key (e.g. ``"brand"``).
        verdict: match / review / mismatch / na.
        extracted: value read off the label (raw, as extracted), or None.
        expected: value from the application, or None.
        similarity: numeric similarity/closeness where meaningful
            (fuzzy score 0-100, or None for exact/na checks).
        reason: one human-readable sentence explaining the verdict.
        detail: matcher-specific extras (parsed values, clause diffs, ...).
        confidence: extraction confidence 0-1, attached by the engine.
    """

    field: str
    verdict: Verdict
    extracted: str | None
    expected: str | None
    similarity: float | None
    reason: str
    detail: dict[str, Any] | None = None
    confidence: float | None = None


@dataclass
class ExtractedLabel:
    """Fields read from a label image by an Extractor.

    All values are transcribed as printed (capitalization preserved) — the
    rules engine owns all normalization and judgment.
    """

    brand: str | None = None
    class_type: str | None = None
    alcohol_content: str | None = None
    net_contents: str | None = None
    producer: str | None = None
    origin_country: str | None = None
    government_warning: str | None = None
    warning_prefix_appears_bold: bool | None = None
    confidence: dict[str, float] = field(default_factory=dict)
    label_detected: bool = True


@dataclass
class ApplicationData:
    """What the applicant claims on the COLA-style application form."""

    brand: str
    class_type: str | None = None
    abv: str | None = None
    net_contents: str | None = None
    producer: str | None = None
    origin_country: str | None = None
    is_import: bool = False
