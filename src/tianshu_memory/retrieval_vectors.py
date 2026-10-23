"""Validated normalized vectors shared by retrieval and its derived index writer."""

import math

from .domain import require


def vectors(value, count):
    require(isinstance(value, list) and len(value) == count, "dependency_unavailable", 503)
    result = []
    dimension = None
    for vector in value:
        require(
            isinstance(vector, list) and 1 <= len(vector) <= 8192, "dependency_unavailable", 503
        )
        require(
            all(type(n) in (int, float) and math.isfinite(n) for n in vector),
            "dependency_unavailable",
            503,
        )
        length = math.hypot(*vector)
        require(length > 0 and math.isfinite(length), "dependency_unavailable", 503)
        dimension = dimension or len(vector)
        require(len(vector) == dimension, "dependency_unavailable", 503)
        result.append(tuple(n / length for n in vector))
    return result
