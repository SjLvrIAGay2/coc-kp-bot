"""JSON value equality: object order is irrelevant; booleans are not numbers."""
from __future__ import annotations

import math


def json_values_equal(left, right):
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if left is None or right is None:
        return left is right
    if isinstance(left, dict) and isinstance(right, dict):
        return (left.keys() == right.keys()
                and all(isinstance(key, str) for key in (*left, *right))
                and all(json_values_equal(value, right[key]) for key, value in left.items()))
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(json_values_equal(a, b) for a, b in zip(left, right))
    if type(left) in (int, float) and type(right) in (int, float):
        # JSON has one number kind. Equivalent spellings such as1 and1.0
        # remain reusable; non-finite values never form valid JSON evidence.
        return (not isinstance(left, float) or math.isfinite(left)) and (
            not isinstance(right, float) or math.isfinite(right)) and left == right
    return isinstance(left, str) and isinstance(right, str) and left == right
