"""QA gate 1 — adversarial parser inputs for F3 (ABV) and F4 (net contents).

Locks: unusual-but-real label formats parse correctly; garbage degrades to
review (never a silent verdict, never a crash).
"""

from __future__ import annotations

import pytest

from app.models import Verdict
from app.rules.alcohol import match_alcohol, parse_abv
from app.rules.net_contents import match_net_contents, parse_net_contents_ml


class TestAbvFormats:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("45.0 % ALC/VOL", 45.0),
            ("ALC. 45% BY VOL.", 45.0),
            ("90proof", 45.0),
            ("90 PROOF", 45.0),
            ("90° Proof", 45.0),
            ("13.5% alc/vol", 13.5),
            ("45", 45.0),  # bare number = ABV percent (application-form style)
        ],
    )
    def test_real_world_variants_parse(self, text: str, expected: float):
        assert parse_abv(text) == pytest.approx(expected)

    @pytest.mark.parametrize(
        "text",
        [
            "forty-five percent",
            "％",             # lone full-width percent sign
            "４５％",          # full-width digits + full-width percent: unsupported
            "proof",
            "",
            "   ",
        ],
    )
    def test_garbage_never_parses_to_a_number_or_crashes(self, text: str):
        assert parse_abv(text) is None

    def test_unparseable_label_is_review_never_silent(self):
        assert match_alcohol("４５％", "45%").verdict is Verdict.REVIEW

    def test_whitespace_only_label_value_is_mismatch_not_crash(self):
        assert match_alcohol("   ", "45%").verdict is Verdict.MISMATCH

    def test_empty_string_label_value_is_mismatch_not_crash(self):
        assert match_alcohol("", "45%").verdict is Verdict.MISMATCH


class TestNetContentsFormats:
    @pytest.mark.parametrize(
        ("text", "expected_ml"),
        [
            ("1 L", 1000.0),
            ("1000ml", 1000.0),
            ("25.4 fl. oz.", 751.17),
            ("0.75L", 750.0),
            ("0,7 L", 700.0),  # comma decimal
            ("70 cl", 700.0),
            ("330 mL", 330.0),
        ],
    )
    def test_real_world_variants_parse(self, text: str, expected_ml: float):
        assert parse_net_contents_ml(text) == pytest.approx(expected_ml, abs=0.01)

    @pytest.mark.parametrize("text", ["750", "a fifth", "12 oz", "", "  ", "mL"])
    def test_unitless_or_garbage_returns_none(self, text: str):
        # bare '12 oz' (no 'fl') is deliberately unsupported -> None -> review path
        assert parse_net_contents_ml(text) is None

    def test_unparseable_label_is_review_never_silent(self):
        assert match_net_contents("a fifth", "750 mL").verdict is Verdict.REVIEW

    def test_unparseable_application_is_review_never_silent(self):
        assert match_net_contents("750 mL", "a fifth").verdict is Verdict.REVIEW

    def test_cross_unit_equalities(self):
        assert match_net_contents("0.75L", "750 mL").verdict is Verdict.MATCH
        assert match_net_contents("1000ml", "1 L").verdict is Verdict.MATCH
        assert match_net_contents("25.4 fl. oz.", "750 mL").verdict is Verdict.MATCH
