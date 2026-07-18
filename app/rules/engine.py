"""Verification engine: runs all 7 field matchers and aggregates an overall status.

Also enforces R6 (robustness): any field whose extraction confidence falls
below `LOW_CONFIDENCE_THRESHOLD` is downgraded to review — a low-confidence
read never surfaces as a silent match or mismatch.
"""

from __future__ import annotations

from app.models import ApplicationData, ExtractedLabel, FieldResult, Verdict
from app.rules.alcohol import match_alcohol
from app.rules.net_contents import match_net_contents
from app.rules.origin import match_origin
from app.rules.producer import match_producer
from app.rules.text_match import match_brand, match_class_type
from app.rules.warning import match_warning

LOW_CONFIDENCE_THRESHOLD = 0.6


def _as_text(value: object) -> str | None:
    """Defensive boundary: a schema-violating payload may carry non-string values."""
    if value is None or isinstance(value, str):
        return value
    return str(value)


def _not_provided(field: str, extracted: str | None) -> FieldResult:
    return FieldResult(
        field=field,
        verdict=Verdict.NA,
        extracted=extracted,
        expected=None,
        similarity=None,
        reason="Not provided in the application — nothing to verify.",
    )


def _apply_confidence(
    result: FieldResult, confidence: float | None, extracted: str | None = None
) -> FieldResult:
    """Attach extraction confidence; downgrade decisive verdicts when it is low.

    The downgrade only applies to uncertain readings of text that IS on the
    label. A confidently absent field (extracted None, confidence 0) must keep
    its decisive verdict — e.g. an import with no origin statement is a
    mismatch, not a review.
    """
    result.confidence = confidence
    if (
        confidence is not None
        and confidence < LOW_CONFIDENCE_THRESHOLD
        and extracted is not None
        and result.verdict in (Verdict.MATCH, Verdict.MISMATCH)
    ):
        result.verdict = Verdict.REVIEW
        result.reason += (
            f" Extraction confidence was low ({confidence:.0%}) — needs human review."
        )
    return result


def verify(extracted: ExtractedLabel, application: ApplicationData) -> list[FieldResult]:
    """Run every field matcher and return per-field results in a stable order."""
    conf = extracted.confidence
    brand = _as_text(extracted.brand)
    class_type = _as_text(extracted.class_type)
    alcohol_content = _as_text(extracted.alcohol_content)
    net_contents = _as_text(extracted.net_contents)
    producer = _as_text(extracted.producer)
    origin_country = _as_text(extracted.origin_country)
    government_warning = _as_text(extracted.government_warning)

    results = [
        _apply_confidence(match_brand(brand, application.brand), conf.get("brand"), brand),
        _apply_confidence(
            match_class_type(class_type, application.class_type)
            if application.class_type
            else _not_provided("class_type", class_type),
            conf.get("class_type"),
            class_type,
        ),
        _apply_confidence(
            match_alcohol(alcohol_content, application.abv)
            if application.abv
            else _not_provided("abv", alcohol_content),
            conf.get("alcohol_content"),
            alcohol_content,
        ),
        _apply_confidence(
            match_net_contents(net_contents, application.net_contents)
            if application.net_contents
            else _not_provided("net_contents", net_contents),
            conf.get("net_contents"),
            net_contents,
        ),
        _apply_confidence(
            match_producer(producer, application.producer)
            if application.producer
            else _not_provided("producer", producer),
            conf.get("producer"),
            producer,
        ),
        _apply_confidence(
            match_origin(origin_country, application.origin_country, application.is_import),
            conf.get("origin_country"),
            origin_country,
        ),
        _apply_confidence(
            match_warning(government_warning, extracted.warning_prefix_appears_bold),
            conf.get("government_warning"),
            government_warning,
        ),
    ]
    return results


def overall_status(results: list[FieldResult]) -> str:
    """Aggregate: any mismatch -> 'mismatch'; else any review -> 'review'; else 'match'."""
    verdicts = {r.verdict for r in results}
    if Verdict.MISMATCH in verdicts:
        return Verdict.MISMATCH.value
    if Verdict.REVIEW in verdicts:
        return Verdict.REVIEW.value
    return Verdict.MATCH.value
