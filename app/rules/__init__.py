"""Deterministic rules engine — one module per field concern.

No verdict in this package is ever produced by an LLM (core architecture
decision in docs/SPEC.md): every matcher is pure, deterministic Python.
"""

from app.rules.engine import (
    LOW_CONFIDENCE_THRESHOLD,
    build_result_payload,
    overall_status,
    overall_status_with_required,
    verify,
)

__all__ = [
    "LOW_CONFIDENCE_THRESHOLD",
    "build_result_payload",
    "overall_status",
    "overall_status_with_required",
    "verify",
]
