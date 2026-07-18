"""F1 brand name and F2 class/type designation — fuzzy text matchers.

SPEC.md: case-insensitive, whitespace-normalized, rapidfuzz token_sort_ratio.
>= 90 -> match, 75-89 -> review, < 75 -> mismatch.
"""

from __future__ import annotations

from rapidfuzz import fuzz

from app.models import FieldResult, Verdict
from app.rules.normalize import casefold_norm

MATCH_THRESHOLD = 90.0
REVIEW_THRESHOLD = 75.0


def _fuzzy_match(field: str, label: str, extracted: str | None, expected: str) -> FieldResult:
    if extracted is None or not extracted.strip():
        return FieldResult(
            field=field,
            verdict=Verdict.MISMATCH,
            extracted=extracted,
            expected=expected,
            similarity=0.0,
            reason=f"No {label} found on the label.",
        )

    score = fuzz.token_sort_ratio(casefold_norm(extracted), casefold_norm(expected))
    if score >= MATCH_THRESHOLD:
        verdict = Verdict.MATCH
        reason = f"{label.capitalize()} matches the application (similarity {score:.0f}/100)."
    elif score >= REVIEW_THRESHOLD:
        verdict = Verdict.REVIEW
        reason = (
            f"{label.capitalize()} is close but not identical "
            f"(similarity {score:.0f}/100) — needs human review."
        )
    else:
        verdict = Verdict.MISMATCH
        reason = (
            f"{label.capitalize()} on the label does not match the application "
            f"(similarity {score:.0f}/100)."
        )
    return FieldResult(
        field=field,
        verdict=verdict,
        extracted=extracted,
        expected=expected,
        similarity=round(score, 1),
        reason=reason,
    )


def match_brand(extracted: str | None, expected: str) -> FieldResult:
    """F1: brand name.

    Labels often surround the brand with taglines or series text the vision
    model may fold in ("DRY CREEK BENCH Redlands Ranch"). When the words of the
    application's brand are fully present but extra words drag the ordered
    similarity down, that is a review case — never a hard mismatch.
    """
    result = _fuzzy_match("brand", "brand name", extracted, expected)
    if result.verdict is Verdict.MISMATCH and extracted and extracted.strip():
        containment = fuzz.token_set_ratio(casefold_norm(extracted), casefold_norm(expected))
        if containment >= MATCH_THRESHOLD:
            result.verdict = Verdict.REVIEW
            result.reason = (
                "The application's brand name appears on the label alongside extra "
                "wording — needs human review to confirm which text is the brand."
            )
    return result


def match_class_type(extracted: str | None, expected: str) -> FieldResult:
    """F2: class/type designation (e.g. 'Kentucky Straight Bourbon Whiskey')."""
    return _fuzzy_match("class_type", "class/type designation", extracted, expected)
