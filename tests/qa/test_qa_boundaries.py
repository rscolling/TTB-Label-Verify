"""QA gate 1 — boundary-value contract locks (WP2+ must not break these).

Exact-threshold behavior: similarity 90.0 and 75.0, confidence 0.60, ABV
diff exactly 0.05, net-contents diff exactly 1%.
"""

from __future__ import annotations

from rapidfuzz import fuzz

from app.models import ApplicationData, ExtractedLabel, Verdict
from app.rules import verify
from app.rules.alcohol import match_alcohol
from app.rules.net_contents import match_net_contents
from app.rules.text_match import match_brand


class TestFuzzyThresholdBoundaries:
    def test_similarity_exactly_90_is_match(self):
        # token_sort_ratio('aaaaaaaaab', 'aaaaaaaaac') == 90.0 exactly
        assert fuzz.token_sort_ratio("aaaaaaaaab", "aaaaaaaaac") == 90.0
        result = match_brand("aaaaaaaaab", "aaaaaaaaac")
        assert result.verdict is Verdict.MATCH
        assert result.similarity == 90.0

    def test_similarity_exactly_75_is_review(self):
        # token_sort_ratio('aaab', 'aaac') == 75.0 exactly
        assert fuzz.token_sort_ratio("aaab", "aaac") == 75.0
        result = match_brand("aaab", "aaac")
        assert result.verdict is Verdict.REVIEW
        assert result.similarity == 75.0

    def test_similarity_just_below_75_is_mismatch(self):
        # token_sort_ratio('aab', 'aac') == 66.7 -> below the review band
        result = match_brand("aab", "aac")
        assert result.verdict is Verdict.MISMATCH


class TestConfidenceBoundary:
    def _verify_brand(self, confidence: float) -> Verdict:
        extracted = ExtractedLabel(brand="Stone's Throw", confidence={"brand": confidence})
        results = verify(extracted, ApplicationData(brand="Stone's Throw"))
        return results[0].verdict

    def test_confidence_exactly_at_threshold_keeps_verdict(self):
        # engine rule is strictly-below 0.6 -> 0.60 exactly is NOT downgraded
        assert self._verify_brand(0.6) is Verdict.MATCH

    def test_confidence_just_below_threshold_downgrades_to_review(self):
        assert self._verify_brand(0.5999) is Verdict.REVIEW

    def test_low_confidence_never_downgrades_na(self):
        extracted = ExtractedLabel(
            brand="Stone's Throw", confidence={"brand": 0.9, "origin_country": 0.05}
        )
        results = verify(extracted, ApplicationData(brand="Stone's Throw", is_import=False))
        origin = next(r for r in results if r.field == "origin_country")
        assert origin.verdict is Verdict.NA


class TestAbvToleranceBoundary:
    def test_difference_exactly_tolerance_matches(self):
        assert match_alcohol("45.05%", "45%").verdict is Verdict.MATCH
        assert match_alcohol("44.95%", "45%").verdict is Verdict.MATCH

    def test_difference_just_over_tolerance_mismatches(self):
        assert match_alcohol("45.06%", "45%").verdict is Verdict.MISMATCH

    def test_proof_boundary_after_conversion(self):
        # 90.1 proof = 45.05% -> exactly at tolerance vs 45%
        assert match_alcohol("90.1 Proof", "45%").verdict is Verdict.MATCH
        assert match_alcohol("90.2 Proof", "45%").verdict is Verdict.MISMATCH


class TestNetContentsToleranceBoundary:
    def test_exactly_one_percent_matches(self):
        assert match_net_contents("757.5 mL", "750 mL").verdict is Verdict.MATCH
        assert match_net_contents("742.5 mL", "750 mL").verdict is Verdict.MATCH

    def test_just_over_one_percent_mismatches(self):
        assert match_net_contents("757.6 mL", "750 mL").verdict is Verdict.MISMATCH
