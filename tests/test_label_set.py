"""L1: multi-image label-set merge foundation (QA P2-8)."""

from dataclasses import replace

from app.models import ExtractedLabel
from app.rules.label_set import merge_label_set
from app.rules.warning import CANONICAL_WARNING
from tests.conftest import HIGH_CONFIDENCE


def _lab(**overrides) -> ExtractedLabel:
    base = ExtractedLabel(
        brand="Acme",
        class_type="Vodka",
        alcohol_content="40%",
        net_contents=None,
        producer=None,
        origin_country=None,
        government_warning=None,
        confidence=dict(HIGH_CONFIDENCE),
        label_detected=True,
    )
    return replace(base, **overrides)


class TestMergeLabelSet:
    def test_best_of_picks_higher_confidence(self):
        front = _lab(net_contents=None)
        back = _lab(
            brand=None,
            alcohol_content=None,
            net_contents="750 mL",
            government_warning=CANONICAL_WARNING,
            confidence={**HIGH_CONFIDENCE, "net_contents": 0.99, "government_warning": 0.9},
        )
        merged = merge_label_set([front, back])
        assert merged.merged.brand == "Acme"
        assert merged.merged.net_contents == "750 mL"
        assert merged.merged.government_warning == CANONICAL_WARNING
        assert merged.sources_used["net_contents"] == 1

    def test_contradiction_when_abv_differs(self):
        a = _lab(alcohol_content="40%")
        b = _lab(alcohol_content="45%")
        merged = merge_label_set([a, b])
        fields = {c["field"] for c in merged.contradictions}
        assert "alcohol_content" in fields
