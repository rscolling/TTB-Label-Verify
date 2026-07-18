"""L1: F4 net contents — unit normalization to mL, +/-1% tolerance."""

import pytest

from app.models import Verdict
from app.rules.net_contents import match_net_contents, parse_net_contents_ml


class TestParseNetContents:
    @pytest.mark.parametrize(
        ("text", "expected_ml"),
        [
            ("750 mL", 750.0),
            ("750ML", 750.0),
            ("75 cL", 750.0),
            ("1 L", 1000.0),
            ("0,7 L", 700.0),
            ("1.75 Liters", 1750.0),
            ("25.4 fl oz", 751.17),
            ("25.4 FL. OZ.", 751.17),
            ("12 fluid ounces", 354.88),
        ],
    )
    def test_variants(self, text: str, expected_ml: float):
        assert parse_net_contents_ml(text) == pytest.approx(expected_ml, abs=0.01)

    def test_unparseable_returns_none(self):
        assert parse_net_contents_ml("a fifth") is None


class TestMatchNetContents:
    def test_trap7a_750ml_equals_75cl(self):
        """Trap 7: '750 mL' vs '75 cL' -> match."""
        result = match_net_contents("750 mL", "75 cL")
        assert result.verdict is Verdict.MATCH

    def test_trap7b_750ml_vs_700ml_mismatch(self):
        """Trap 7: '750 mL' vs '700 mL' -> mismatch (7.1% apart)."""
        result = match_net_contents("750 mL", "700 mL")
        assert result.verdict is Verdict.MISMATCH

    def test_fl_oz_within_one_percent_of_750ml(self):
        # 25.4 fl oz = 751.17 mL -> 0.16% from 750 mL
        result = match_net_contents("25.4 fl oz", "750 mL")
        assert result.verdict is Verdict.MATCH

    def test_liter_conversion(self):
        assert match_net_contents("1 L", "1000 mL").verdict is Verdict.MATCH

    def test_missing_net_contents_is_mismatch(self):
        assert match_net_contents(None, "750 mL").verdict is Verdict.MISMATCH

    def test_unparseable_label_value_is_review(self):
        assert match_net_contents("a fifth", "750 mL").verdict is Verdict.REVIEW

    def test_unparseable_application_value_is_review(self):
        assert match_net_contents("750 mL", "standard bottle").verdict is Verdict.REVIEW
