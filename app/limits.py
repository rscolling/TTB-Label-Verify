"""Upload size caps and request guards (QA P0-1).

Reject oversized payloads with friendly 413s *after* a bounded read when
Content-Length is missing, so a public demo cannot hold multi-hundred-MB
bodies in memory indefinitely. Caps are env-tunable for ops.
"""

from __future__ import annotations

import os

# Defaults chosen for label photos + forms: large enough for phone photos,
# small enough to bound memory and Anthropic spend on the public demo.
# Phone photos can be multi-MB; adversarial noise PNGs used in tests are larger.
# Bound is DoS protection, not a substitute for server-side downscale (SPEC R2).
DEFAULT_MAX_IMAGE_BYTES = 40 * 1024 * 1024  # 40 MB per label photo
DEFAULT_MAX_FORM_BYTES = 20 * 1024 * 1024  # 20 MB submittal form
DEFAULT_MAX_BATCH_TOTAL_BYTES = 200 * 1024 * 1024  # 200 MB across one batch request


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        return default


def max_image_bytes() -> int:
    return _env_int("MAX_IMAGE_BYTES", DEFAULT_MAX_IMAGE_BYTES)


def max_form_bytes() -> int:
    return _env_int("MAX_FORM_BYTES", DEFAULT_MAX_FORM_BYTES)


def max_batch_total_bytes() -> int:
    return _env_int("MAX_BATCH_TOTAL_BYTES", DEFAULT_MAX_BATCH_TOTAL_BYTES)


def human_mb(n: int) -> str:
    return f"{n / (1024 * 1024):.0f} MB"
