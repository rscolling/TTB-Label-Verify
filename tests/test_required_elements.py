"""L1: server-side required-elements check (QA P1-4)."""

from dataclasses import replace

from app.models import ApplicationData, ExtractedLabel, Verdict
from app.rules import overall_status_with_required, verify
from app.rules.required_elements import class_family, missing_required, required_elements_payload
from app.rules.warning import CANONICAL_WARNING
from tests.conftest import HIGH_CONFIDENCE


def _label(**overrides) -> ExtractedLabel:
    base = ExtractedLabel(
        brand="Stone's Throw",
        class_type="Kentucky Straight Bourbon Whiskey",
        alcohol_content="45% Alc./Vol.",
        net_contents="750 mL",
        producer="Blue Ridge Distilling Co., Asheville, NC",
        origin_country=None,
        government_warning=CANONICAL_WARNING,
        warning_prefix_appears_bold=True,
        confidence=dict(HIGH_CONFIDENCE),
        label_detected=True,
    )
    return replace(base, **overrides)


def _app(**overrides) -> ApplicationData:
    base = ApplicationData(
        brand="Stone's Throw",
        class_type="Kentucky Straight Bourbon Whiskey",
        abv="45%",
        net_contents="750 mL",
        producer="Blue Ridge Distilling Co., Asheville, NC",
    )
    return replace(base, **overrides)


class TestClassFamily:
    def test_bourbon_is_spirits(self):
        assert class_family("Kentucky Straight Bourbon Whiskey") == "spirits"

    def test_single_malt_whisky_is_spirits_not_malt(self):
        assert class_family("Single Malt Whisky") == "spirits"

    def test_porter_is_malt_not_port_wine(self):
        assert class_family("Porter") == "malt"

    def test_red_wine_is_wine(self):
        assert class_family("Red Wine") == "wine"


class TestMissingRequired:
    def test_complete_spirits_label_has_no_missing(self):
        extracted = _label()
        results = verify(extracted, _app())
        assert missing_required(extracted, results) == []
        assert overall_status_with_required(results, extracted) == "match"

    def test_spirits_missing_abv_reviews_with_fov_wording(self):
        extracted = _label(alcohol_content=None)
        app = _app(abv=None)
        results = verify(extracted, app)
        missing = missing_required(extracted, results)
        keys = [m.key for m in missing]
        assert "abv" in keys
        abv_miss = next(m for m in missing if m.key == "abv")
        assert "5.61" in abv_miss.reason
        assert overall_status_with_required(results, extracted) == "review"

    def test_malt_missing_abv_is_not_required(self):
        extracted = _label(class_type="India Pale Ale", alcohol_content=None)
        app = _app(class_type="India Pale Ale", abv=None)
        results = verify(extracted, app)
        keys = [m.key for m in missing_required(extracted, results)]
        assert "abv" not in keys

    def test_mismatch_not_downgraded_by_required_miss(self):
        extracted = _label(alcohol_content="40% ABV", net_contents=None)
        results = verify(extracted, _app())
        assert any(r.verdict is Verdict.MISMATCH for r in results)
        assert overall_status_with_required(results, extracted) == "mismatch"

    def test_payload_shape(self):
        extracted = _label(net_contents=None)
        results = verify(extracted, _app(net_contents=None))
        payload = required_elements_payload(extracted, results)
        assert payload["family"] == "spirits"
        assert any(m["key"] == "net_contents" for m in payload["missing"])
