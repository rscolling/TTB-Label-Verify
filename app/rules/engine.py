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


def _not_provided(field: str, extracted: str | None) -> FieldResult:
    return FieldResult(
        field=field,
        verdict=Verdict.NA,
        extracted=extracted,
        expected=None,
        similarity=None,
        reason="Not provided in the application — nothing to verify.",
    )


def _apply_confidence(result: FieldResult, confidence: float | None) -> FieldResult:
    """Attach extraction confidence; downgrade decisive verdicts when it is low."""
    result.confidence = confidence
    if (
        confidence is not None
        and confidence < LOW_CONFIDENCE_THRESHOLD
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

    results = [
        _apply_confidence(match_brand(extracted.brand, application.brand), conf.get("brand")),
        _apply_confidence(
            match_class_type(extracted.class_type, application.class_type)
            if application.class_type
            else _not_provided("class_type", extracted.class_type),
            conf.get("class_type"),
        ),
        _apply_confidence(
            match_alcohol(extracted.alcohol_content, application.abv)
            if application.abv
            else _not_provided("abv", extracted.alcohol_content),
            conf.get("alcohol_content"),
        ),
        _apply_confidence(
            match_net_contents(extracted.net_contents, application.net_contents)
            if application.net_contents
            else _not_provided("net_contents", extracted.net_contents),
            conf.get("net_contents"),
        ),
        _apply_confidence(
            match_producer(extracted.producer, application.producer)
            if application.producer
            else _not_provided("producer", extracted.producer),
            conf.get("producer"),
        ),
        _apply_confidence(
            match_origin(extracted.origin_country, application.origin_country, application.is_import),
            conf.get("origin_country"),
        ),
        _apply_confidence(
            match_warning(extracted.government_warning, extracted.warning_prefix_appears_bold),
            conf.get("government_warning"),
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
