"""L1: F1 brand and F2 class/type fuzzy matchers (SPEC.md thresholds 90/75)."""

from app.models import Verdict
from app.rules.text_match import match_brand, match_class_type


class TestBrand:
    def test_trap1_brand_case_insensitive_stones_throw(self):
        """Trap 1: STONE'S THROW vs Stone's Throw -> match."""
        result = match_brand("STONE'S THROW", "Stone's Throw")
        assert result.verdict is Verdict.MATCH
        assert result.similarity == 100.0

    def test_curly_apostrophe_and_whitespace_normalized(self):
        result = match_brand("Stone’s   Throw", "Stone's Throw")
        assert result.verdict is Verdict.MATCH

    def test_word_order_ignored_by_token_sort(self):
        result = match_brand("Throw Stone's", "Stone's Throw")
        assert result.verdict is Verdict.MATCH

    def test_minor_typo_still_matches_at_90(self):
        # token_sort_ratio('stones thraw', 'stones throw') ~= 91.7
        result = match_brand("Stones Thraw", "Stones Throw")
        assert result.verdict is Verdict.MATCH

    def test_review_band_75_to_89(self):
        # token_sort_ratio ~= 88.9 -> review, not silent match or mismatch
        result = match_brand("Blue Ridge Distilling Company", "Blue Ridge Distilling Co.")
        assert result.verdict is Verdict.REVIEW
        assert 75 <= result.similarity < 90

    def test_mismatch_below_75(self):
        result = match_brand("Old Oak Reserve", "Stone's Throw")
        assert result.verdict is Verdict.MISMATCH
        assert result.similarity < 75

    def test_missing_brand_is_mismatch(self):
        result = match_brand(None, "Stone's Throw")
        assert result.verdict is Verdict.MISMATCH
        assert "no brand name found" in result.reason.lower()

    def test_result_carries_values_and_reason(self):
        result = match_brand("STONE'S THROW", "Stone's Throw")
        assert result.extracted == "STONE'S THROW"
        assert result.expected == "Stone's Throw"
        assert result.reason


class TestClassType:
    def test_exact_match(self):
        result = match_class_type("Kentucky Straight Bourbon Whiskey", "kentucky straight bourbon whiskey")
        assert result.verdict is Verdict.MATCH

    def test_spelling_variant_matches(self):
        result = match_class_type("Kentucky Straight Bourbon Whisky", "Kentucky Straight Bourbon Whiskey")
        assert result.verdict is Verdict.MATCH

    def test_partial_designation_is_review(self):
        # token_sort_ratio ~= 84.2 -> review band
        result = match_class_type("Straight Bourbon Whiskey", "Kentucky Straight Bourbon Whiskey")
        assert result.verdict is Verdict.REVIEW

    def test_wrong_class_is_mismatch(self):
        result = match_class_type("India Pale Ale", "Kentucky Straight Bourbon Whiskey")
        assert result.verdict is Verdict.MISMATCH

    def test_missing_class_is_mismatch(self):
        result = match_class_type("", "Kentucky Straight Bourbon Whiskey")
        assert result.verdict is Verdict.MISMATCH


class TestBrandContainmentFallback:
    """Live-eval finding: vision model folded a label tagline into the brand."""

    def test_brand_with_tagline_folded_in_is_review_not_mismatch(self):
        result = match_brand("Dry Creek Bench Redlands Ranch", "Redlands Ranch")
        assert result.verdict is Verdict.REVIEW
        assert "extra wording" in result.reason

    def test_genuinely_different_brand_stays_mismatch(self):
        result = match_brand("Copper Hollow", "Redlands Ranch")
        assert result.verdict is Verdict.MISMATCH
