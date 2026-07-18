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


class TestBottlerStatementBoilerplate:
    """Live-eval finding: labels carry bottler statements the application omits."""

    def test_distilled_and_bottled_by_prefix_still_matches(self):
        result = match_producer(
            "Distilled and Bottled by Copper Hollow Distilling Co., 412 Millrace Road, Bardstown, Kentucky 40004",
            "Copper Hollow Distilling Co., 412 Millrace Road, Bardstown, Kentucky 40004",
        )
        assert result.verdict is Verdict.MATCH

    def test_all_caps_produced_and_bottled_by(self):
        result = match_producer(
            "PRODUCED AND BOTTLED BY Mariner's Cove Winery, 88 Harbor Lane, Astoria, Oregon 97103",
            "Mariner's Cove Winery, 88 Harbor Lane, Astoria, Oregon 97103",
        )
        assert result.verdict is Verdict.MATCH

    def test_imported_by_prefix(self):
        result = match_producer(
            "Imported by Thamesgate Spirits Ltd., London, England",
            "Thamesgate Spirits Ltd., London, England",
        )
        assert result.verdict is Verdict.MATCH

    def test_boilerplate_on_both_sides(self):
        result = match_producer(
            "Bottled by Copper Hollow Distilling Co., Bardstown, Kentucky",
            "Distilled and Bottled by Copper Hollow Distilling Co., Bardstown, Kentucky",
        )
        assert result.verdict is Verdict.MATCH

    def test_company_actually_named_by_the_barrel_is_not_over_stripped(self):
        result = match_producer(
            "By The Barrel Brewing Co., 12 Main St, Dayton, Ohio",
            "By The Barrel Brewing Co., 12 Main St, Dayton, Ohio",
        )
        assert result.verdict is Verdict.MATCH

    def test_brewed_and_canned_by_prefix(self):
        result = match_producer(
            "BREWED AND CANNED BY GRANITE LEDGE BREWING CO., 27 SWITCHBACK TRAIL, MISSOULA, MONTANA 59802",
            "Granite Ledge Brewing Co., 27 Switchback Trail, Missoula, Montana 59802",
        )
        assert result.verdict is Verdict.MATCH
