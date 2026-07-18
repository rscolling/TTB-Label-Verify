"""L1: F7 government health warning — exact match, caps rule, clause diff, bold."""

from app.models import Verdict
from app.rules.warning import CANONICAL_WARNING, match_warning


class TestMatchWarning:
    def test_exact_statutory_text_matches(self):
        result = match_warning(CANONICAL_WARNING, prefix_appears_bold=True)
        assert result.verdict is Verdict.MATCH
        assert result.detail["prefix_all_caps"] is True

    def test_trap2_title_case_prefix_fails(self):
        """Trap 2: 'Government Warning:' (title case) -> mismatch via caps rule."""
        label_text = CANONICAL_WARNING.replace("GOVERNMENT WARNING:", "Government Warning:")
        result = match_warning(label_text, prefix_appears_bold=True)
        assert result.verdict is Verdict.MISMATCH
        assert result.detail["prefix_all_caps"] is False
        assert "capital letters" in result.reason

    def test_trap3_one_word_changed_fails_with_clause_diff(self):
        """Trap 3: 'might cause health problems' -> mismatch with clause (2) diff."""
        label_text = CANONICAL_WARNING.replace("may cause health problems", "might cause health problems")
        result = match_warning(label_text, prefix_appears_bold=True)
        assert result.verdict is Verdict.MISMATCH
        diff = result.detail["clause_diff"]
        assert [d["clause"] for d in diff] == ["(2)"]
        assert any("may" in difference and "might" in difference for difference in diff[0]["differences"])

    def test_trap4_line_breaks_double_spaces_nbsp_normalized(self):
        """Trap 4: line breaks, double spaces, non-breaking spaces -> match."""
        label_text = (
            CANONICAL_WARNING.replace("GOVERNMENT WARNING: ", "GOVERNMENT WARNING:\n")
            .replace("during pregnancy", "during pregnancy")  # non-breaking space
            .replace(". (2)", ".  \n(2)")  # double space + line break
        )
        result = match_warning(label_text, prefix_appears_bold=True)
        assert result.verdict is Verdict.MATCH

    def test_smart_quote_normalization_in_warning_text(self):
        """Trap 4 (quotes): typographic apostrophes must not defeat the exact match."""
        # The statutory text has no apostrophes, so simulate a transcription that
        # renders "Surgeon General" with typographic quotes around it.
        label_text = CANONICAL_WARNING.replace("Surgeon General", "“Surgeon General”")
        straight = CANONICAL_WARNING.replace("Surgeon General", '"Surgeon General"')
        smart = match_warning(label_text, prefix_appears_bold=True)
        plain = match_warning(straight, prefix_appears_bold=True)
        # Both normalize identically: same verdict, same similarity.
        assert smart.verdict is plain.verdict
        assert smart.similarity == plain.similarity

    def test_missing_clause_reported_in_diff(self):
        label_text = CANONICAL_WARNING.split(" (2)")[0]  # clause (2) dropped entirely
        result = match_warning(label_text, prefix_appears_bold=True)
        assert result.verdict is Verdict.MISMATCH
        assert any(d["clause"] == "(2)" and d["found"] is None for d in result.detail["clause_diff"])

    def test_missing_warning_is_mismatch(self):
        result = match_warning(None, prefix_appears_bold=None)
        assert result.verdict is Verdict.MISMATCH
        assert "16.21" in result.reason

    def test_not_bold_prefix_downgrades_to_review(self):
        result = match_warning(CANONICAL_WARNING, prefix_appears_bold=False)
        assert result.verdict is Verdict.REVIEW
        assert "bold" in result.reason

    def test_unknown_boldness_does_not_block_match(self):
        result = match_warning(CANONICAL_WARNING, prefix_appears_bold=None)
        assert result.verdict is Verdict.MATCH

    def test_all_caps_body_is_accepted(self):
        # 16.21 mandates caps only for the prefix; an all-caps body is compliant.
        result = match_warning(CANONICAL_WARNING.upper(), prefix_appears_bold=True)
        assert result.verdict is Verdict.MATCH
