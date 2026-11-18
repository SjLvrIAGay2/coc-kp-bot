"""Exact candidate text containment, without rewriting or inferred equivalence."""
from __future__ import annotations


def contained_candidate_text(values: list[str]) -> str:
    """Return an existing value only if it contains every complete input value.

    The caller must retain all candidate identities, sources and scores. There
    is no trimming, normalization, fragment stitching or fallback concatenation.
    """
    if not values or any(not isinstance(value, str) for value in values):
        raise ValueError("contained_text_requires_strings")
    original = max(values, key=len)
    if any(value not in original for value in values):
        raise ValueError("contained_text_requires_enclosing_original")
    return original
