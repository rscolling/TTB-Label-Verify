"""Text normalization shared by the field matchers."""

from __future__ import annotations

import re

# Typographic characters that OCR/vision transcription may produce, mapped to
# their plain-ASCII equivalents so exact comparisons are not defeated by
# typography (TESTING.md trap 4).
_CHAR_MAP = str.maketrans(
    {
        "‘": "'",  # left single quote
        "’": "'",  # right single quote / apostrophe
        "‛": "'",
        "“": '"',  # left double quote
        "”": '"',  # right double quote
        "„": '"',
        "–": "-",  # en dash
        "—": "-",  # em dash
        " ": " ",  # non-breaking space
    }
)

_WS_RE = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    """Normalize quotes/dashes and collapse all whitespace runs to one space."""
    return _WS_RE.sub(" ", text.translate(_CHAR_MAP)).strip()


def casefold_norm(text: str) -> str:
    """Case-insensitive comparison form of `normalize_text`."""
    return normalize_text(text).casefold()
