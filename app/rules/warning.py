"""F7 Government Health Warning (27 CFR 16.21).

SPEC.md rules:
- Exact text match after whitespace/quote normalization.
- The "GOVERNMENT WARNING:" prefix must be ALL CAPS on the label (16.21
  mandates capital letters for the prefix; the body's letter case is not
  mandated, so body comparison is case-insensitive).
- Bold prefix is best-effort via vision-model self-report — a documented
  limitation, so a "not bold" report downgrades to review, never a hard fail.
- On text mismatch, report a per-clause diff.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

from rapidfuzz import fuzz

from app.models import FieldResult, Verdict
from app.rules.normalize import normalize_text

# Statutory text per 27 CFR 16.21 (matches ttb.gov; also quoted in docs/SPEC.md).
CANONICAL_WARNING = (
    "GOVERNMENT WARNING: (1) According to the Surgeon General, women should not "
    "drink alcoholic beverages during pregnancy because of the risk of birth "
    "defects. (2) Consumption of alcoholic beverages impairs your ability to "
    "drive a car or operate machinery, and may cause health problems."
)

_PREFIX_RE = re.compile(r"government\s+warning\s*:", re.IGNORECASE)
_CLAUSE_SPLIT_RE = re.compile(r"\(\s*([12])\s*\)")


def _split_clauses(text: str) -> dict[str, str]:
    """Split a warning into {'prefix': ..., '(1)': ..., '(2)': ...} (missing keys omitted)."""
    parts = _CLAUSE_SPLIT_RE.split(text)
    clauses = {"prefix": parts[0].strip()}
    for marker, body in zip(parts[1::2], parts[2::2]):
        clauses[f"({marker})"] = body.strip()
    return clauses


def _word_diff(expected: str, found: str) -> list[str]:
    """Human-readable word-level differences, e.g. ["expected 'may' -> found 'might'"]."""
    exp_words = expected.split()
    got_words = found.split()
    diffs: list[str] = []
    matcher = SequenceMatcher(a=[w.casefold() for w in exp_words], b=[w.casefold() for w in got_words])
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        if op == "equal":
            continue
        exp_part = " ".join(exp_words[i1:i2])
        got_part = " ".join(got_words[j1:j2])
        if op == "replace":
            diffs.append(f"expected '{exp_part}' -> found '{got_part}'")
        elif op == "delete":
            diffs.append(f"missing '{exp_part}'")
        else:  # insert
            diffs.append(f"unexpected '{got_part}'")
    return diffs


def _clause_diff(expected: str, found: str) -> list[dict[str, object]]:
    """Per-clause comparison of the canonical warning vs the label's text."""
    expected_clauses = _split_clauses(expected)
    found_clauses = _split_clauses(found)
    diff: list[dict[str, object]] = []
    for key, exp_clause in expected_clauses.items():
        got_clause = found_clauses.get(key)
        if got_clause is None:
            diff.append({"clause": key, "expected": exp_clause, "found": None, "differences": ["clause missing"]})
        elif exp_clause.casefold() != got_clause.casefold():
            diff.append(
                {
                    "clause": key,
                    "expected": exp_clause,
                    "found": got_clause,
                    "differences": _word_diff(exp_clause, got_clause),
                }
            )
    return diff


def match_warning(extracted: str | None, prefix_appears_bold: bool | None) -> FieldResult:
    """F7: verify the government warning against the statutory text."""
    expected = CANONICAL_WARNING
    if extracted is None or not extracted.strip():
        return FieldResult(
            field="government_warning",
            verdict=Verdict.MISMATCH,
            extracted=extracted,
            expected=expected,
            similarity=0.0,
            reason="No government health warning found on the label (required by 27 CFR 16.21).",
        )

    norm_label = normalize_text(extracted)
    norm_expected = normalize_text(expected)
    similarity = round(fuzz.ratio(norm_label.casefold(), norm_expected.casefold()), 1)

    # Caps check on the case-preserved text: the prefix as printed must be all caps.
    prefix_match = _PREFIX_RE.search(norm_label)
    caps_ok = prefix_match is not None and prefix_match.group(0) == prefix_match.group(0).upper()

    text_ok = norm_label.casefold() == norm_expected.casefold()

    if not text_ok:
        diff = _clause_diff(norm_expected, norm_label)
        summary = "; ".join(
            d["differences"][0] if d["differences"] else str(d["clause"]) for d in diff[:2]
        )
        reason = f"Warning text differs from the statutory text: {summary or 'see clause diff'}."
        if not caps_ok:
            reason += ' Also, the "GOVERNMENT WARNING:" prefix is not in all capital letters.'
        return FieldResult(
            field="government_warning",
            verdict=Verdict.MISMATCH,
            extracted=extracted,
            expected=expected,
            similarity=similarity,
            reason=reason,
            detail={"clause_diff": diff, "prefix_all_caps": caps_ok},
        )

    if not caps_ok:
        found_prefix = prefix_match.group(0) if prefix_match else "(prefix not found)"
        return FieldResult(
            field="government_warning",
            verdict=Verdict.MISMATCH,
            extracted=extracted,
            expected=expected,
            similarity=similarity,
            reason=(
                'The warning text is correct, but the "GOVERNMENT WARNING:" prefix must '
                f"be in all capital letters — the label shows '{found_prefix}'."
            ),
            detail={"clause_diff": [], "prefix_all_caps": False},
        )

    if prefix_appears_bold is False:
        return FieldResult(
            field="government_warning",
            verdict=Verdict.REVIEW,
            extracted=extracted,
            expected=expected,
            similarity=similarity,
            reason=(
                'Warning text and capitalization are correct, but the "GOVERNMENT WARNING:" '
                "prefix may not be printed in bold type — needs human review "
                "(bold detection is best-effort; documented limitation)."
            ),
            detail={"clause_diff": [], "prefix_all_caps": True, "prefix_appears_bold": False},
        )

    return FieldResult(
        field="government_warning",
        verdict=Verdict.MATCH,
        extracted=extracted,
        expected=expected,
        similarity=similarity,
        reason="Government warning matches the statutory text, with the prefix in all capital letters.",
        detail={"clause_diff": [], "prefix_all_caps": True, "prefix_appears_bold": prefix_appears_bold},
    )
