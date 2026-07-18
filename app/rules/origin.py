"""F6 country of origin.

SPEC.md: only verified when the application marks the product as an import;
case-insensitive exact after normalization; absent on a domestic product is
N/A, not a failure.
"""

from __future__ import annotations

import re

from app.models import FieldResult, Verdict
from app.rules.normalize import casefold_norm

_PRODUCT_OF_RE = re.compile(r"^(?:product|produce)\s+of\s+", re.IGNORECASE)


def _normalize_country(text: str) -> str:
    """Comparison form: normalized, casefolded, 'Product of X' -> 'x'."""
    return _PRODUCT_OF_RE.sub("", casefold_norm(text)).strip(" .")


def match_origin(extracted: str | None, expected: str | None, is_import: bool) -> FieldResult:
    """F6: country of origin, gated on the application's import flag."""
    if not is_import:
        return FieldResult(
            field="origin_country",
            verdict=Verdict.NA,
            extracted=extracted,
            expected=expected,
            similarity=None,
            reason="Domestic product — a country of origin statement is not required.",
        )

    if expected is None or not expected.strip():
        return FieldResult(
            field="origin_country",
            verdict=Verdict.REVIEW,
            extracted=extracted,
            expected=expected,
            similarity=None,
            reason=(
                "Application marks this as an import but gives no country of origin "
                "— needs human review."
            ),
        )

    if extracted is None or not extracted.strip():
        return FieldResult(
            field="origin_country",
            verdict=Verdict.MISMATCH,
            extracted=extracted,
            expected=expected,
            similarity=None,
            reason=(
                "Imported product, but no country of origin statement was found on "
                "the label (required for imports)."
            ),
        )

    if _normalize_country(extracted) == _normalize_country(expected):
        return FieldResult(
            field="origin_country",
            verdict=Verdict.MATCH,
            extracted=extracted,
            expected=expected,
            similarity=None,
            reason=f"Country of origin matches the application ({expected.strip()}).",
        )
    return FieldResult(
        field="origin_country",
        verdict=Verdict.MISMATCH,
        extracted=extracted,
        expected=expected,
        similarity=None,
        reason=(
            f"Country of origin differs: label says {extracted!r}, "
            f"application says {expected!r}."
        ),
    )
