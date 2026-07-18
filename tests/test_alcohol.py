"""L1: F3 alcohol content — parsing variants, proof conversion, tolerance."""

import pytest

from app.models import Verdict
from app.rules.alcohol import match_alcohol, parse_abv


class TestParseAbv:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("45% Alc./Vol.", 45.0),
            ("45% ABV", 45.0),
            ("90 Proof", 45.0),
            ("45%", 45.0),
            ("45", 45.0),
            ("Alc. 45% by Vol.", 45.0),
            ("ALC 40.5% BY VOL", 40.5),
            ("86.6 proof", 43.3),
            ("90proof", 45.0),
        ],
    )
    def test_variants(self, text: str, expected: float):
        assert parse_abv(text) == pytest.approx(expected)

    def test_unparseable_returns_none(self):
        assert parse_abv("forty-five percent") is None


class TestMatchAlcohol:
    def test_trap5_90_proof_matches_45_percent(self):
        """Trap 5: '90 Proof' label vs '45%' application -> match via conversion."""
        result = match_alcohol("90 Proof", "45%")
        assert result.verdict is Verdict.MATCH
        assert result.detail["label_abv"] == 45.0

    def test_trap6_40_abv_vs_45_mismatch(self):
        """Trap 6: '40% ABV' label vs '45%' application -> mismatch."""
        result = match_alcohol("40% ABV", "45%")
        assert result.verdict is Verdict.MISMATCH
        assert "40" in result.reason and "45" in result.reason

    def test_within_tolerance_matches(self):
        assert match_alcohol("45.05%", "45%").verdict is Verdict.MATCH

    def test_just_outside_tolerance_mismatches(self):
        assert match_alcohol("45.1%", "45%").verdict is Verdict.MISMATCH

    def test_proof_abv_disagreement_on_label_vs_application(self):
        # Label says 80 proof (40%) but application claims 45% -> mismatch.
        assert match_alcohol("80 Proof", "45% ABV").verdict is Verdict.MISMATCH

    def test_missing_alcohol_content_is_mismatch(self):
        result = match_alcohol(None, "45%")
        assert result.verdict is Verdict.MISMATCH

    def test_unparseable_label_value_is_review_not_silent_verdict(self):
        result = match_alcohol("forty-five percent", "45%")
        assert result.verdict is Verdict.REVIEW

    def test_unparseable_application_value_is_review(self):
        result = match_alcohol("45%", "high")
        assert result.verdict is Verdict.REVIEW
