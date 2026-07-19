"""Multi-image label-set merge (QA P2-8 foundation).

A real COLA submission is often a set (front / back / neck). This module
takes several ExtractedLabel readings and produces:
  - best-of each field (highest confidence where text is present)
  - cross-image contradictions (same field, different decisive text)

Not yet wired into the HTTP batch API (one row still = one photo). Callers
and future work packages can group uploads by submission_id and run this
pure function before verify().
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.models import ExtractedLabel
from app.rules.normalize import casefold_norm

_FIELDS = (
    "brand",
    "class_type",
    "alcohol_content",
    "net_contents",
    "producer",
    "origin_country",
    "government_warning",
)


@dataclass
class FieldReading:
    value: str
    confidence: float
    source_index: int


@dataclass
class LabelSetMerge:
    merged: ExtractedLabel
    contradictions: list[dict[str, Any]] = field(default_factory=list)
    sources_used: dict[str, int] = field(default_factory=dict)


def _confidence(label: ExtractedLabel, field_name: str) -> float:
    return float(label.confidence.get(field_name, 0.0))


def merge_label_set(labels: list[ExtractedLabel]) -> LabelSetMerge:
    """Best-of merge across images; record contradictory non-empty readings."""
    if not labels:
        raise ValueError("merge_label_set requires at least one ExtractedLabel")
    if len(labels) == 1:
        return LabelSetMerge(merged=labels[0], contradictions=[], sources_used={})

    best: dict[str, FieldReading] = {}
    per_field_values: dict[str, list[FieldReading]] = {f: [] for f in _FIELDS}

    for index, label in enumerate(labels):
        for fname in _FIELDS:
            value = getattr(label, fname)
            if value is None or not str(value).strip():
                continue
            reading = FieldReading(
                value=str(value).strip(),
                confidence=_confidence(label, fname),
                source_index=index,
            )
            per_field_values[fname].append(reading)
            current = best.get(fname)
            if current is None or reading.confidence > current.confidence:
                best[fname] = reading

    contradictions: list[dict[str, Any]] = []
    for fname, readings in per_field_values.items():
        norms = {casefold_norm(r.value) for r in readings}
        if len(norms) > 1:
            contradictions.append(
                {
                    "field": fname,
                    "values": [
                        {
                            "value": r.value,
                            "confidence": r.confidence,
                            "source_index": r.source_index,
                        }
                        for r in readings
                    ],
                }
            )

    # Bold: true if any source reports bold; None if all unknown.
    bold_votes = [lab.warning_prefix_appears_bold for lab in labels]
    if any(v is True for v in bold_votes):
        bold = True
    elif all(v is False for v in bold_votes):
        bold = False
    else:
        bold = next((v for v in bold_votes if v is not None), None)

    merged = ExtractedLabel(
        brand=best["brand"].value if "brand" in best else None,
        class_type=best["class_type"].value if "class_type" in best else None,
        alcohol_content=best["alcohol_content"].value if "alcohol_content" in best else None,
        net_contents=best["net_contents"].value if "net_contents" in best else None,
        producer=best["producer"].value if "producer" in best else None,
        origin_country=best["origin_country"].value if "origin_country" in best else None,
        government_warning=(
            best["government_warning"].value if "government_warning" in best else None
        ),
        warning_prefix_appears_bold=bold,
        confidence={f: best[f].confidence for f in best},
        label_detected=any(lab.label_detected for lab in labels),
    )
    sources = {f: best[f].source_index for f in best}
    return LabelSetMerge(merged=merged, contradictions=contradictions, sources_used=sources)
