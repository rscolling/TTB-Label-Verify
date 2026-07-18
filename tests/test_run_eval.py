"""Offline unit tests for eval/run_eval.py: comparison, reporting, exit codes.

The harness's live path (ClaudeExtractor) is exercised only by a real
key-bearing run; here every test injects a stub extractor or checks the
fail-fast behavior when the key is absent. No network, no API key.
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from app.models import ExtractedLabel
from app.rules.warning import CANONICAL_WARNING
from tests.conftest import HIGH_CONFIDENCE

REPO_ROOT = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location("run_eval", REPO_ROOT / "eval" / "run_eval.py")
assert _spec is not None and _spec.loader is not None
run_eval = importlib.util.module_from_spec(_spec)
sys.modules["run_eval"] = run_eval
_spec.loader.exec_module(run_eval)

# Extraction that matches manifest label 01 (Copper Hollow bourbon) exactly.
LABEL_01_EXTRACTION = ExtractedLabel(
    brand="COPPER HOLLOW",
    class_type="Kentucky Straight Bourbon Whiskey",
    alcohol_content="45% Alc./Vol.",
    net_contents="750 mL",
    producer="Copper Hollow Distilling Co., 412 Millrace Road, Bardstown, Kentucky 40004",
    origin_country=None,
    government_warning=CANONICAL_WARNING,
    warning_prefix_appears_bold=True,
    confidence=dict(HIGH_CONFIDENCE),
    label_detected=True,
)


class StubExtractor:
    def __init__(self, result: ExtractedLabel | None = None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error

    def extract(self, image_bytes: bytes) -> ExtractedLabel:
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


class TestCompareVerdicts:
    def test_all_expected_pass(self):
        actual = {"brand": "match", "government_warning": "match"}
        expected = {"brand": ["match"], "warning": ["match", "review"]}
        checks = run_eval.compare_verdicts(actual, expected)
        assert all(check["ok"] for check in checks)
        assert checks[1]["field"] == "government_warning"  # manifest key mapping

    def test_unexpected_verdict_flagged(self):
        checks = run_eval.compare_verdicts({"brand": "mismatch"}, {"brand": ["match"]})
        assert checks == [
            {
                "manifest_key": "brand",
                "field": "brand",
                "actual": "mismatch",
                "allowed": ["match"],
                "ok": False,
            }
        ]

    def test_review_allowed_for_robustness_labels(self):
        """Trap-10 entries allow either match or review; review must pass."""
        checks = run_eval.compare_verdicts({"brand": "review"}, {"brand": ["match", "review"]})
        assert checks[0]["ok"]

    def test_missing_field_is_a_failure(self):
        checks = run_eval.compare_verdicts({}, {"abv": ["match"]})
        assert not checks[0]["ok"]
        assert checks[0]["actual"] == "<missing>"


class TestBuildApplication:
    def test_numeric_abv_becomes_clean_string(self):
        entry = run_eval.load_manifest()[0]
        application = run_eval.build_application(entry)
        assert application.brand == "Copper Hollow"
        assert application.abv == "45"
        assert application.is_import is False

    def test_import_entry_carries_origin(self):
        entries = run_eval.load_manifest()
        gin = next(entry for entry in entries if "gin-import" in entry["file"])
        application = run_eval.build_application(gin)
        assert application.is_import is True
        assert application.origin_country == "England"


class TestEvaluateLabel:
    def test_matching_extraction_passes_label_01(self):
        entry = run_eval.load_manifest()[0]
        record = run_eval.evaluate_label(entry, StubExtractor(LABEL_01_EXTRACTION))
        assert record["error"] is None
        assert record["latency_s"] is not None
        assert run_eval.record_passed(record)

    def test_wrong_abv_fails_the_label(self):
        entry = run_eval.load_manifest()[0]
        wrong = replace(LABEL_01_EXTRACTION, alcohol_content="40% Alc./Vol.")
        record = run_eval.evaluate_label(entry, StubExtractor(wrong))
        assert not run_eval.record_passed(record)
        failed = [check for check in record["checks"] if not check["ok"]]
        assert [check["manifest_key"] for check in failed] == ["abv"]

    def test_extractor_exception_recorded_not_raised(self):
        entry = run_eval.load_manifest()[0]
        record = run_eval.evaluate_label(entry, StubExtractor(error=RuntimeError("boom")))
        assert record["error"] == "RuntimeError: boom"
        assert not run_eval.record_passed(record)


class TestRenderReport:
    def make_records(self):
        entry = run_eval.load_manifest()[0]
        good = run_eval.evaluate_label(entry, StubExtractor(LABEL_01_EXTRACTION))
        bad = run_eval.evaluate_label(
            entry, StubExtractor(replace(LABEL_01_EXTRACTION, alcohol_content="40%"))
        )
        return [good, bad]

    def test_plain_report_has_matrix_and_totals(self):
        report = run_eval.render_report(self.make_records())
        assert "01-bourbon-clean" in report
        assert "Labels passing: 1/2" in report
        assert "Field verdicts as expected: 13/14" in report
        assert "✗ mismatch (want match)" in report
        assert "Latency per label" in report
        assert "budget 5s" in report

    def test_markdown_report_is_a_pipe_table(self):
        report = run_eval.render_report(self.make_records(), markdown=True)
        lines = report.splitlines()
        assert lines[0].startswith("| label | brand |")
        assert lines[0].rstrip().endswith("| time |")
        assert set(lines[1].replace("|", "")) == {"-"}
        assert "**✗ mismatch (want match)**" in report

    def test_error_record_renders_as_error_row(self):
        entry = run_eval.load_manifest()[0]
        record = run_eval.evaluate_label(entry, StubExtractor(error=RuntimeError("boom")))
        report = run_eval.render_report([record])
        assert "ERROR: RuntimeError: boom" in report
        assert "Labels with errors: 1" in report


class TestMain:
    def test_exit_zero_when_all_expected(self, capsys):
        code = run_eval.main(["--limit", "1"], extractor=StubExtractor(LABEL_01_EXTRACTION))
        assert code == 0
        out = capsys.readouterr().out
        assert "Labels passing: 1/1" in out

    def test_exit_nonzero_on_unexpected_verdict(self, capsys):
        wrong = StubExtractor(replace(LABEL_01_EXTRACTION, alcohol_content="40%"))
        code = run_eval.main(["--limit", "1"], extractor=wrong)
        assert code == 1
        assert "want match" in capsys.readouterr().out

    def test_label_filter_selects_by_name(self, capsys):
        code = run_eval.main(["--label", "01-bourbon-clean"], extractor=StubExtractor(LABEL_01_EXTRACTION))
        assert code == 0
        out = capsys.readouterr().out
        assert "Labels passing: 1/1" in out

    def test_unmatched_filter_exits_2(self, capsys):
        code = run_eval.main(["--label", "no-such-label"], extractor=StubExtractor(LABEL_01_EXTRACTION))
        assert code == 2
        assert "No labels matched" in capsys.readouterr().err

    def test_missing_api_key_fails_fast_with_clear_message(self, capsys, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        code = run_eval.main(["--limit", "1"])  # no injected extractor -> live path
        assert code == 2
        err = capsys.readouterr().err
        assert "ANTHROPIC_API_KEY is not set" in err
        assert "python eval/run_eval.py" in err

    @pytest.mark.live
    def test_live_smoke_one_label(self):
        """Real API spot-check (1 call). Skipped without a key."""
        import os

        if not os.environ.get("ANTHROPIC_API_KEY"):
            pytest.skip("ANTHROPIC_API_KEY not set")
        assert run_eval.main(["--limit", "1"]) == 0
