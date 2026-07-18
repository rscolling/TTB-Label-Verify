"""L1: F5 producer — fuzzy name, warn-level address token overlap."""

from app.models import Verdict
from app.rules.producer import match_producer

EXPECTED = "Blue Ridge Distilling Co., 12 Main Street, Asheville, NC 28801"


class TestMatchProducer:
    def test_exact_producer_matches(self):
        result = match_producer(EXPECTED, EXPECTED)
        assert result.verdict is Verdict.MATCH

    def test_name_case_and_ampersand_variants_match(self):
        # token_sort_ratio('smith and sons winery', 'smith & sons winery') = 90.0
        result = match_producer("SMITH AND SONS WINERY, 1 Vine Rd, Napa, CA", "Smith & Sons Winery, 1 Vine Rd, Napa, CA")
        assert result.verdict is Verdict.MATCH

    def test_close_name_is_review(self):
        # token_sort_ratio('smith winery', 'smith & sons winery') ~= 77.4
        result = match_producer("Smith Winery, 1 Vine Rd, Napa, CA", "Smith & Sons Winery, 1 Vine Rd, Napa, CA")
        assert result.verdict is Verdict.REVIEW

    def test_different_name_is_mismatch(self):
        result = match_producer("High Sierra Spirits, 9 Peak Ave, Reno, NV", EXPECTED)
        assert result.verdict is Verdict.MISMATCH

    def test_address_divergence_is_warn_level_only_never_mismatch(self):
        # Same name, completely different address -> review, not mismatch (SPEC F5).
        result = match_producer("Blue Ridge Distilling Co., 900 Elsewhere Blvd, Portland, OR 97201", EXPECTED)
        assert result.verdict is Verdict.REVIEW
        assert result.detail["address_token_overlap"] < 0.5

    def test_reformatted_address_with_shared_tokens_still_matches(self):
        result = match_producer("Blue Ridge Distilling Co., 12 Main St., Asheville NC 28801", EXPECTED)
        assert result.verdict is Verdict.MATCH

    def test_no_address_on_either_side_skips_address_check(self):
        result = match_producer("Blue Ridge Distilling Co.", "Blue Ridge Distilling Co.")
        assert result.verdict is Verdict.MATCH
        assert result.detail["address_token_overlap"] is None

    def test_missing_producer_is_mismatch(self):
        assert match_producer(None, EXPECTED).verdict is Verdict.MISMATCH
