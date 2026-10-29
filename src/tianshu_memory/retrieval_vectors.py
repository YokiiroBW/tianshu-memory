"""Validated normalized vectors shared by retrieval and its derived index writer."""

import math
import sys
from array import array

from .domain import require


def pack(vector):
    result = array("f", vector)
    if sys.byteorder != "little":
        result.byteswap()
    return result.tobytes()


def unpack(value):
    require(isinstance(value, (bytes, memoryview)), "dependency_unavailable", 503)
    require(0 < len(value) <= 8192 * 4 and len(value) % 4 == 0, "dependency_unavailable", 503)
    result = array("f")
    result.frombytes(bytes(value))
    if sys.byteorder != "little":
        result.byteswap()
    require(
        all(math.isfinite(n) for n in result) and math.hypot(*result) > 0,
        "dependency_unavailable",
        503,
    )
    return result


def dot(left, right):
    require(len(left) == len(right), "dependency_unavailable", 503)
    return sum(a * b for a, b in zip(left, right, strict=True))


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
