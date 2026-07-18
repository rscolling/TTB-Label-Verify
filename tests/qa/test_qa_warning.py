"""QA gate 1 — adversarial F7 government-warning cases beyond the build suite.

Locks: punctuation drift fails, missing clause markers fail with a usable
diff, unicode whitespace normalizes, lowercase prefix fails even when the
text is otherwise perfect.
"""

from __future__ import annotations

from app.models import Verdict
from app.rules.warning import CANONICAL_WARNING, match_warning


class TestWarningTextDrift:
    def test_trailing_period_removed_is_mismatch_with_clause2_diff(self):
        result = match_warning(CANONICAL_WARNING[:-1], prefix_appears_bold=True)
        assert result.verdict is Verdict.MISMATCH
        assert [d["clause"] for d in result.detail["clause_diff"]] == ["(2)"]

    def test_missing_clause_markers_is_mismatch(self):
        # Same words, but the mandatory "(1)"/"(2)" enumeration is missing.
        no_markers = CANONICAL_WARNING.replace("(1) ", "").replace("(2) ", "")
        result = match_warning(no_markers, prefix_appears_bold=True)
        assert result.verdict is Verdict.MISMATCH
        missing = {d["clause"] for d in result.detail["clause_diff"]}
        assert "(1)" in missing and "(2)" in missing

    def test_clauses_swapped_is_mismatch(self):
        clause1 = (
            "According to the Surgeon General, women should not drink alcoholic "
            "beverages during pregnancy because of the risk of birth defects."
        )
        clause2 = (
            "Consumption of alcoholic beverages impairs your ability to drive a "
            "car or operate machinery, and may cause health problems."
        )
        swapped = f"GOVERNMENT WARNING: (1) {clause2} (2) {clause1}"
        assert match_warning(swapped, prefix_appears_bold=True).verdict is Verdict.MISMATCH

    def test_extra_marketing_sentence_appended_is_mismatch(self):
        padded = CANONICAL_WARNING + " Please drink responsibly."
        assert match_warning(padded, prefix_appears_bold=True).verdict is Verdict.MISMATCH


class TestWarningNormalizationEdges:
    def test_nbsp_everywhere_still_matches(self):
        nbsp_text = CANONICAL_WARNING.replace(" ", " ")
        assert match_warning(nbsp_text, prefix_appears_bold=True).verdict is Verdict.MATCH

    def test_tabs_and_crlf_line_breaks_still_match(self):
        mangled = CANONICAL_WARNING.replace(". (2)", ".\r\n\t(2)")
        assert match_warning(mangled, prefix_appears_bold=True).verdict is Verdict.MATCH

    def test_whitespace_only_extraction_is_mismatch_not_crash(self):
        assert match_warning("   \n ", prefix_appears_bold=None).verdict is Verdict.MISMATCH


class TestWarningCapsRule:
    def test_entirely_lowercase_warning_is_mismatch(self):
        # Body text is case-insensitive, but the prefix as printed must be caps.
        result = match_warning(CANONICAL_WARNING.lower(), prefix_appears_bold=True)
        assert result.verdict is Verdict.MISMATCH
        assert result.detail["prefix_all_caps"] is False

    def test_mixed_case_prefix_is_mismatch_even_with_perfect_body(self):
        label = CANONICAL_WARNING.replace("GOVERNMENT WARNING:", "GOVERNMENT Warning:")
        result = match_warning(label, prefix_appears_bold=True)
        assert result.verdict is Verdict.MISMATCH

    def test_all_caps_body_with_caps_prefix_matches(self):
        # 27 CFR 16.21 mandates caps for the prefix only; full-caps labels exist.
        assert match_warning(CANONICAL_WARNING.upper(), prefix_appears_bold=True).verdict is Verdict.MATCH

    def test_wrong_text_and_bad_caps_reports_both(self):
        label = CANONICAL_WARNING.replace(
            "GOVERNMENT WARNING:", "Government Warning:"
        ).replace("may cause", "might cause")
        result = match_warning(label, prefix_appears_bold=True)
        assert result.verdict is Verdict.MISMATCH
        assert result.detail["prefix_all_caps"] is False
        assert result.detail["clause_diff"]
