"""L1: engine — orchestration, N/A for absent application fields, confidence
downgrade (trap 10), overall status aggregation."""

from dataclasses import replace

from app.models import ApplicationData, ExtractedLabel, Verdict
from app.rules import overall_status, verify


def application(**overrides) -> ApplicationData:
    base = ApplicationData(
        brand="Stone's Throw",
        class_type="Kentucky Straight Bourbon Whiskey",
        abv="45%",
        net_contents="750 mL",
        producer="Blue Ridge Distilling Co., 12 Main Street, Asheville, NC 28801",
        origin_country=None,
        is_import=False,
    )
    return replace(base, **overrides)


def by_field(results):
    return {r.field: r for r in results}


class TestVerify:
    def test_all_fields_present_in_stable_order(self, good_extraction):
        results = verify(good_extraction, application())
        assert [r.field for r in results] == [
            "brand",
            "class_type",
            "abv",
            "net_contents",
            "producer",
            "origin_country",
            "government_warning",
        ]

    def test_clean_extraction_fully_matches(self, good_extraction):
        results = verify(good_extraction, application())
        fields = by_field(results)
        assert fields["origin_country"].verdict is Verdict.NA  # domestic
        for name in ("brand", "class_type", "abv", "net_contents", "producer", "government_warning"):
            assert fields[name].verdict is Verdict.MATCH, name
        assert overall_status(results) == "match"

    def test_absent_application_fields_are_na_not_failures(self, good_extraction):
        results = verify(good_extraction, application(class_type=None, abv=None, net_contents=None, producer=None))
        fields = by_field(results)
        for name in ("class_type", "abv", "net_contents", "producer"):
            assert fields[name].verdict is Verdict.NA, name
            assert "not provided" in fields[name].reason.lower()

    def test_confidence_attached_to_results(self, good_extraction):
        fields = by_field(verify(good_extraction, application()))
        assert fields["brand"].confidence == 0.98

    def test_trap10_low_confidence_match_downgrades_to_review(self, good_extraction):
        """Trap 10: a low-confidence extraction never surfaces as a silent match."""
        good_extraction.confidence["brand"] = 0.4
        fields = by_field(verify(good_extraction, application()))
        assert fields["brand"].verdict is Verdict.REVIEW
        assert "confidence" in fields["brand"].reason.lower()

    def test_trap10_low_confidence_mismatch_also_downgrades_to_review(self, good_extraction):
        """Trap 10: ...and never as a silent mismatch either."""
        good_extraction.brand = "Completely Different Brand"
        good_extraction.confidence["brand"] = 0.3
        fields = by_field(verify(good_extraction, application()))
        assert fields["brand"].verdict is Verdict.REVIEW

    def test_low_confidence_does_not_touch_na(self, good_extraction):
        good_extraction.confidence["origin_country"] = 0.1
        fields = by_field(verify(good_extraction, application()))
        assert fields["origin_country"].verdict is Verdict.NA

    def test_missing_confidence_leaves_verdict_alone(self, good_extraction):
        good_extraction.confidence = {}
        fields = by_field(verify(good_extraction, application()))
        assert fields["brand"].verdict is Verdict.MATCH
        assert fields["brand"].confidence is None


class TestOverallStatus:
    def test_any_mismatch_wins(self, good_extraction):
        good_extraction.alcohol_content = "40% ABV"
        results = verify(good_extraction, application())
        assert overall_status(results) == "mismatch"

    def test_review_when_no_mismatch(self, good_extraction):
        good_extraction.confidence["brand"] = 0.4
        results = verify(good_extraction, application())
        assert overall_status(results) == "review"

    def test_match_when_clean(self, good_extraction):
        assert overall_status(verify(good_extraction, application())) == "match"


def test_confidently_absent_field_keeps_decisive_verdict():
    """Live-eval finding: origin absent on an import (confidence 0) must stay a
    mismatch — the low-confidence downgrade applies only to uncertain readings
    of text that is present."""
    extracted = ExtractedLabel(
        brand="Copper Hollow",
        class_type=None,
        alcohol_content=None,
        net_contents=None,
        producer=None,
        origin_country=None,
        government_warning=None,
        warning_prefix_appears_bold=None,
        confidence={"origin_country": 0.0},
        label_detected=True,
    )
    application = ApplicationData(
        brand="Copper Hollow",
        class_type=None,
        abv=None,
        net_contents=None,
        producer=None,
        origin_country="Product of France",
        is_import=True,
    )
    results = {r.field: r for r in verify(extracted, application)}
    assert results["origin_country"].verdict is Verdict.MISMATCH
