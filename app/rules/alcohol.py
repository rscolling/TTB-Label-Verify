"""F3 alcohol content — parse ABV/proof variants, convert, compare numerically.

SPEC.md: parse `45% Alc./Vol.`, `45% ABV`, `90 Proof`, `45%` variants.
Proof = 2 x ABV. Numeric equality with +/-0.05 tolerance after conversion.
"""

from __future__ import annotations

import re

from app.models import FieldResult, Verdict
from app.rules.normalize import normalize_text

ABV_TOLERANCE = 0.05
_EPSILON = 1e-9

_PROOF_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:°\s*)?proof\b", re.IGNORECASE)
_PERCENT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")
_BARE_NUMBER_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*$")


def parse_abv(text: str) -> float | None:
    """Parse an alcohol-content string to ABV percent, or None if unparseable.

    Handles proof ('90 Proof' -> 45.0), percent variants ('45% Alc./Vol.',
    '45% ABV', 'Alc. 45% by Vol.', '45%'), and bare numbers ('45' -> 45.0,
    treated as ABV percent — the application form commonly omits the sign).
    """
    t = normalize_text(text)
    if m := _PROOF_RE.search(t):
        return float(m.group(1)) / 2.0
    if m := _PERCENT_RE.search(t):
        return float(m.group(1))
    if m := _BARE_NUMBER_RE.match(t):
        return float(m.group(1))
    return None


def match_alcohol(extracted: str | None, expected: str) -> FieldResult:
    """F3: compare label alcohol content against the application, in ABV percent."""
    if extracted is None or not extracted.strip():
        return FieldResult(
            field="abv",
            verdict=Verdict.MISMATCH,
            extracted=extracted,
            expected=expected,
            similarity=None,
            reason="No alcohol content found on the label.",
        )

    label_abv = parse_abv(extracted)
    expected_abv = parse_abv(expected)
    if expected_abv is None:
        return FieldResult(
            field="abv",
            verdict=Verdict.REVIEW,
            extracted=extracted,
            expected=expected,
            similarity=None,
            reason=f"Could not interpret the application's alcohol content ({expected!r}) — needs human review.",
        )
    if label_abv is None:
        return FieldResult(
            field="abv",
            verdict=Verdict.REVIEW,
            extracted=extracted,
            expected=expected,
            similarity=None,
            reason=f"Could not interpret the label's alcohol content ({extracted!r}) — needs human review.",
        )

    diff = abs(label_abv - expected_abv)
    detail = {"label_abv": label_abv, "expected_abv": expected_abv, "difference": round(diff, 4)}
    if diff <= ABV_TOLERANCE + _EPSILON:
        verdict = Verdict.MATCH
        reason = f"Alcohol content matches: label reads {label_abv:g}% ABV, application says {expected_abv:g}%."
    else:
        verdict = Verdict.MISMATCH
        reason = (
            f"Alcohol content differs: label reads {label_abv:g}% ABV, "
            f"application says {expected_abv:g}% (difference {diff:g} exceeds ±{ABV_TOLERANCE})."
        )
    return FieldResult(
        field="abv",
        verdict=verdict,
        extracted=extracted,
        expected=expected,
        similarity=None,
        reason=reason,
        detail=detail,
    )
