"""F4 net contents — unit normalization to mL, compare with +/-1% tolerance.

SPEC.md: normalize mL / cL / L / fl oz -> compare in mL, +/-1% tolerance.
Also American standard malt measures (QA P1-6): pint, quart, and compound
statements like "1 PT. 6 FL. OZ." (27 CFR part 7 net contents practice).
"""

from __future__ import annotations

import re

from app.models import FieldResult, Verdict
from app.rules.normalize import normalize_text

RELATIVE_TOLERANCE = 0.01  # +/-1% of the application value
ML_PER_FL_OZ = 29.5735
ML_PER_PINT = 16 * ML_PER_FL_OZ  # US liquid pint
ML_PER_QUART = 32 * ML_PER_FL_OZ  # US liquid quart

# Compound American standard: "1 PT. 6 FL. OZ." / "1 PT 6 FL OZ"
_COMPOUND_PT_FL_OZ_RE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*p(?:in)?t\.?\s*"
    r"(\d+(?:[.,]\d+)?)\s*(?:fluid\s+ounces?|fl\.?\s*oz\.?)",
    re.IGNORECASE,
)
_COMPOUND_QT_FL_OZ_RE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*q(?:uar)?t\.?\s*"
    r"(\d+(?:[.,]\d+)?)\s*(?:fluid\s+ounces?|fl\.?\s*oz\.?)",
    re.IGNORECASE,
)

# Longest alternatives first so 'fl oz' wins over bare 'l', 'cl' over 'l', etc.
# Pint/quart listed before bare 'pt' collisions with other tokens.
_QUANTITY_RE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*"
    r"(fluid\s+ounces?|fl\.?\s*oz\.?|milliliters?|millilitres?|centiliters?|centilitres?"
    r"|liters?|litres?|quarts?|pints?|ml|cl|qt|pt|l)\b",
    re.IGNORECASE,
)

_UNIT_TO_ML = {
    "ml": 1.0,
    "milliliter": 1.0,
    "millilitre": 1.0,
    "cl": 10.0,
    "centiliter": 10.0,
    "centilitre": 10.0,
    "l": 1000.0,
    "liter": 1000.0,
    "litre": 1000.0,
    "floz": ML_PER_FL_OZ,
    "fluidounce": ML_PER_FL_OZ,
    "pt": ML_PER_PINT,
    "pint": ML_PER_PINT,
    "qt": ML_PER_QUART,
    "quart": ML_PER_QUART,
}


def parse_net_contents_ml(text: str) -> float | None:
    """Parse a net-contents string to mL (metric, fl oz, US pint/quart, compounds)."""
    normalized = normalize_text(text)

    # Compound American standard first (pint + fl oz, quart + fl oz).
    if m := _COMPOUND_PT_FL_OZ_RE.search(normalized):
        pints = float(m.group(1).replace(",", "."))
        floz = float(m.group(2).replace(",", "."))
        return pints * ML_PER_PINT + floz * ML_PER_FL_OZ
    if m := _COMPOUND_QT_FL_OZ_RE.search(normalized):
        quarts = float(m.group(1).replace(",", "."))
        floz = float(m.group(2).replace(",", "."))
        return quarts * ML_PER_QUART + floz * ML_PER_FL_OZ

    m = _QUANTITY_RE.search(normalized)
    if not m:
        return None
    value = float(m.group(1).replace(",", "."))
    unit_key = re.sub(r"[^a-z]", "", m.group(2).lower()).rstrip("s")
    # "pt" from "pts" / "pint" from "pints" after rstrip("s")
    if unit_key == "pint":
        unit_key = "pint"
    elif unit_key == "pin":  # rstrip('s') on "pints" -> "pint" actually; "pts"->"pt"
        unit_key = "pt"
    factor = _UNIT_TO_ML.get(unit_key)
    return value * factor if factor is not None else None


def match_net_contents(extracted: str | None, expected: str) -> FieldResult:
    """F4: compare label net contents against the application, in mL."""
    if extracted is None or not extracted.strip():
        return FieldResult(
            field="net_contents",
            verdict=Verdict.MISMATCH,
            extracted=extracted,
            expected=expected,
            similarity=None,
            reason="No net contents statement found on the label.",
        )

    label_ml = parse_net_contents_ml(extracted)
    expected_ml = parse_net_contents_ml(expected)
    if expected_ml is None:
        return FieldResult(
            field="net_contents",
            verdict=Verdict.REVIEW,
            extracted=extracted,
            expected=expected,
            similarity=None,
            reason=f"Could not interpret the application's net contents ({expected!r}) — needs human review.",
        )
    if label_ml is None:
        return FieldResult(
            field="net_contents",
            verdict=Verdict.REVIEW,
            extracted=extracted,
            expected=expected,
            similarity=None,
            reason=f"Could not interpret the label's net contents ({extracted!r}) — needs human review.",
        )

    if expected_ml == 0:
        return FieldResult(
            field="net_contents",
            verdict=Verdict.REVIEW,
            extracted=extracted,
            expected=expected,
            similarity=None,
            reason="The application lists zero net contents — needs human review.",
        )

    relative_diff = abs(label_ml - expected_ml) / expected_ml
    detail = {
        "label_ml": round(label_ml, 2),
        "expected_ml": round(expected_ml, 2),
        "relative_difference": round(relative_diff, 4),
    }
    if relative_diff <= RELATIVE_TOLERANCE:
        verdict = Verdict.MATCH
        reason = f"Net contents match: label is {label_ml:g} mL, application says {expected_ml:g} mL."
    else:
        verdict = Verdict.MISMATCH
        reason = (
            f"Net contents differ: label is {label_ml:g} mL, application says "
            f"{expected_ml:g} mL ({relative_diff:.1%} apart, tolerance is ±1%)."
        )
    return FieldResult(
        field="net_contents",
        verdict=verdict,
        extracted=extracted,
        expected=expected,
        similarity=None,
        reason=reason,
        detail=detail,
    )
