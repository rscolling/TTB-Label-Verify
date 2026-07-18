"""L1: F6 country of origin — import gating and N/A logic."""

from app.models import Verdict
from app.rules.origin import match_origin


class TestMatchOrigin:
    def test_trap8_domestic_absent_country_is_na_not_fail(self):
        """Trap 8: domestic product, no country on label -> N/A."""
        result = match_origin(None, None, is_import=False)
        assert result.verdict is Verdict.NA

    def test_domestic_with_country_printed_is_still_na(self):
        result = match_origin("Product of USA", None, is_import=False)
        assert result.verdict is Verdict.NA

    def test_trap9_import_missing_country_on_label_fails(self):
        """Trap 9: import per application, no country on label -> mismatch."""
        result = match_origin(None, "France", is_import=True)
        assert result.verdict is Verdict.MISMATCH
        assert "import" in result.reason.lower()

    def test_import_matching_country_case_insensitive(self):
        result = match_origin("FRANCE", "france", is_import=True)
        assert result.verdict is Verdict.MATCH

    def test_import_product_of_prefix_normalized(self):
        result = match_origin("Product of France", "France", is_import=True)
        assert result.verdict is Verdict.MATCH

    def test_import_wrong_country_is_mismatch(self):
        result = match_origin("Product of Spain", "France", is_import=True)
        assert result.verdict is Verdict.MISMATCH

    def test_import_without_expected_country_is_review(self):
        result = match_origin("Product of France", None, is_import=True)
        assert result.verdict is Verdict.REVIEW
