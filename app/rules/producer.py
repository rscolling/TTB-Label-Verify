"""F5 producer name + address.

SPEC.md: case-insensitive fuzzy on the name; address token-overlap is
warn-level only (addresses vary in formatting) — an address discrepancy can
downgrade a match to review, never produce a mismatch on its own.
"""

from __future__ import annotations

import re

from rapidfuzz import fuzz

from app.models import FieldResult, Verdict
from app.rules.normalize import casefold_norm
from app.rules.text_match import MATCH_THRESHOLD, REVIEW_THRESHOLD

ADDRESS_OVERLAP_THRESHOLD = 0.5

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Labels state the producer inside a bottler statement ("DISTILLED AND BOTTLED
# BY ...", "Imported by ..."); applications usually carry the bare name. Strip
# the boilerplate before comparing so it never drags down the name similarity.
_BOILERPLATE_RE = re.compile(
    r"^\s*(?:(?:distilled|produced|bottled|brewed|vinted|cellared|blended|"
    r"imported|manufactured|made|crafted)\s*(?:,|and|&)?\s*)+by\s+",
    re.IGNORECASE,
)


def _strip_bottler_statement(text: str) -> str:
    return _BOILERPLATE_RE.sub("", text, count=1)


def _split_name_address(text: str) -> tuple[str, str]:
    """Split 'Name, street, city' (or newline-separated) into (name, address)."""
    parts = re.split(r"[,\n]", text, maxsplit=1)
    name = parts[0].strip()
    address = parts[1].strip() if len(parts) > 1 else ""
    return name, address


def _address_overlap(label_address: str, expected_address: str) -> float:
    """Fraction of the application's address tokens that appear on the label."""
    expected_tokens = set(_TOKEN_RE.findall(casefold_norm(expected_address)))
    label_tokens = set(_TOKEN_RE.findall(casefold_norm(label_address)))
    if not expected_tokens:
        return 1.0
    return len(expected_tokens & label_tokens) / len(expected_tokens)


def match_producer(extracted: str | None, expected: str) -> FieldResult:
    """F5: fuzzy-match producer name; check address token overlap (warn-level)."""
    if extracted is None or not extracted.strip():
        return FieldResult(
            field="producer",
            verdict=Verdict.MISMATCH,
            extracted=extracted,
            expected=expected,
            similarity=0.0,
            reason="No producer name and address found on the label.",
        )

    label_name, label_address = _split_name_address(_strip_bottler_statement(extracted))
    expected_name, expected_address = _split_name_address(_strip_bottler_statement(expected))

    name_score = fuzz.token_sort_ratio(casefold_norm(label_name), casefold_norm(expected_name))
    overlap = None
    if expected_address and label_address:
        overlap = _address_overlap(label_address, expected_address)

    detail = {
        "name_similarity": round(name_score, 1),
        "address_token_overlap": round(overlap, 2) if overlap is not None else None,
    }

    if name_score < REVIEW_THRESHOLD:
        verdict = Verdict.MISMATCH
        reason = f"Producer name does not match the application (similarity {name_score:.0f}/100)."
    elif name_score < MATCH_THRESHOLD:
        verdict = Verdict.REVIEW
        reason = (
            f"Producer name is close but not identical (similarity {name_score:.0f}/100) "
            "— needs human review."
        )
    elif overlap is not None and overlap < ADDRESS_OVERLAP_THRESHOLD:
        verdict = Verdict.REVIEW
        reason = (
            f"Producer name matches, but the address shares only {overlap:.0%} of the "
            "application's address terms — needs human review (addresses vary in formatting)."
        )
    else:
        verdict = Verdict.MATCH
        reason = f"Producer matches the application (name similarity {name_score:.0f}/100)."

    return FieldResult(
        field="producer",
        verdict=verdict,
        extracted=extracted,
        expected=expected,
        similarity=round(name_score, 1),
        reason=reason,
        detail=detail,
    )
