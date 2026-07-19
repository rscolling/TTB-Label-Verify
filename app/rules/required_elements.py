"""Required-elements check per class family (TTB-cited) — server-side (QA P1-4).

Mirrors the client logic in app/static/app.js so API consumers and the UI
cannot disagree. A required element not found in the photo flags REVIEW,
never FAIL: net contents / producer may be embossed; the warning may sit on
another label of the set.

Broader CFR disclosures (sulfites, FD&C Yellow No. 5, appellation) are
structured here as optional growth checks — they flag REVIEW when the class
family suggests them and the text is absent from extracted fields, without
hard-failing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.models import ExtractedLabel, FieldResult

REQUIRED_MISSING_TEXT = (
    "Not found in this photo — it may appear on another label or be embossed "
    "on the container. Verify before approving."
)
SPIRITS_ABV_FOV_TEXT = (
    "Distilled spirits must show alcohol content in the same field of vision "
    "as the brand and class (27 CFR 5.61) — not found in this photo. "
    "Verify before approving."
)
SULFITES_TEXT = (
    "Wine labels commonly require a sulfite declaration when applicable "
    "(27 CFR 4.32) — not found in this photo. Verify before approving."
)
YELLOW5_TEXT = (
    "Malt beverages that contain FD&C Yellow No. 5 must disclose it "
    "(27 CFR 7.63(b)) — not found in this photo. Verify before approving."
)

CORE_REQUIRED = (
    ("brand", "Brand name"),
    ("class_type", "Kind of drink"),
    ("net_contents", "Amount in bottle"),
    ("producer", "Producer"),
    ("government_warning", "Health warning"),
)
ABV_REQUIRED = ("abv", "Alcohol content")

# Keyword order: spirits before malt so "Single Malt Whisky" is spirits;
# wine "port" is boundary-matched so "Porter" stays malt.
_FAMILY_MATCHERS: list[tuple[str, re.Pattern[str]]] = [
    (
        "spirits",
        re.compile(
            r"(^|[^a-z0-9])(whiskey|whisky|bourbon|rye|vodka|gin|rum|tequila|"
            r"mezcal|brandy|cognac|liqueur|schnapps|spirits?)([^a-z0-9]|$)",
            re.I,
        ),
    ),
    (
        "wine",
        re.compile(
            r"(^|[^a-z0-9])(wine|champagne|sparkling|vermouth|port|sherry|"
            r"riesling|chardonnay|cabernet|zinfandel|merlot|ros[eé])([^a-z0-9]|$)",
            re.I,
        ),
    ),
    (
        "malt",
        re.compile(
            r"(^|[^a-z0-9])(beer|ale|lager|stout|porter|ipa|pilsner|malt)([^a-z0-9]|$)",
            re.I,
        ),
    ),
]


@dataclass(frozen=True)
class MissingElement:
    key: str
    name: str
    reason: str
    severity: str = "review"  # always review for photo-misses


def class_family(class_type_text: str | None) -> str | None:
    if not class_type_text:
        return None
    text = str(class_type_text).lower()
    for family, pattern in _FAMILY_MATCHERS:
        if pattern.search(text):
            return family
    return None


def _present(value: str | None) -> bool:
    return value is not None and str(value).strip() != ""


def _field_extracted(fields: dict[str, FieldResult], key: str) -> str | None:
    result = fields.get(key)
    return result.extracted if result else None


def _field_expected(fields: dict[str, FieldResult], key: str) -> str | None:
    result = fields.get(key)
    return result.expected if result else None


def _blob_from_extraction(extracted: ExtractedLabel) -> str:
    parts = [
        extracted.brand,
        extracted.class_type,
        extracted.alcohol_content,
        extracted.net_contents,
        extracted.producer,
        extracted.origin_country,
        extracted.government_warning,
    ]
    return " ".join(p for p in parts if p).lower()


def missing_required(
    extracted: ExtractedLabel,
    field_results: list[FieldResult],
) -> list[MissingElement]:
    """Return TTB core required elements not found on the label photo."""
    fields = {r.field: r for r in field_results}
    declared = _field_expected(fields, "class_type")
    family = class_family(declared or extracted.class_type)

    required: list[tuple[str, str]] = list(CORE_REQUIRED)
    if family in ("spirits", "wine"):
        required.append(ABV_REQUIRED)

    missing: list[MissingElement] = []
    for key, name in required:
        value = _field_extracted(fields, key)
        if key == "abv" and not _present(value):
            value = extracted.alcohol_content
        if key == "government_warning" and not _present(value):
            value = extracted.government_warning
        if _present(value):
            continue
        reason = REQUIRED_MISSING_TEXT
        if (
            family == "spirits"
            and key == "abv"
            and _present(extracted.brand)
            and _present(extracted.class_type)
        ):
            reason = SPIRITS_ABV_FOV_TEXT
        missing.append(MissingElement(key=key, name=name, reason=reason))

    return missing


def disclosure_hints(extracted: ExtractedLabel, field_results: list[FieldResult]) -> list[MissingElement]:
    """Optional growth-path CFR disclosures (sulfites). Opt-in only — not auto-applied.

    These are conditional on formula/jurisdiction; auto-flagging every wine
    would flood REVIEW. Callers can surface them as informational hints.
    """
    fields = {r.field: r for r in field_results}
    declared = _field_expected(fields, "class_type")
    family = class_family(declared or extracted.class_type)
    blob = _blob_from_extraction(extracted)
    hints: list[MissingElement] = []
    if family == "wine" and "sulfite" not in blob:
        hints.append(
            MissingElement(key="sulfites", name="Sulfite declaration", reason=SULFITES_TEXT)
        )
    if family == "malt" and "yellow no. 5" in blob:
        # Only when Yellow 5 is mentioned without a clear declaration pattern —
        # reserved for future formula-aware checks.
        if "fd&c" not in blob and "yellow no. 5" not in blob:
            hints.append(
                MissingElement(key="yellow5", name="FD&C Yellow No. 5", reason=YELLOW5_TEXT)
            )
    return hints


def apply_required_to_status(overall: str, missing: list[MissingElement]) -> str:
    """Compose with worst-issue logic: never downgrade mismatch; upgrade match→review."""
    if not missing:
        return overall
    if overall == "mismatch":
        return overall
    if overall == "review":
        return overall
    return "review"


def required_elements_payload(
    extracted: ExtractedLabel,
    field_results: list[FieldResult],
) -> dict[str, Any]:
    fields = {r.field: r for r in field_results}
    declared = _field_expected(fields, "class_type")
    family = class_family(declared or extracted.class_type)
    missing = missing_required(extracted, field_results)
    return {
        "family": family,
        "missing": [
            {"key": m.key, "name": m.name, "reason": m.reason, "severity": m.severity}
            for m in missing
        ],
    }
