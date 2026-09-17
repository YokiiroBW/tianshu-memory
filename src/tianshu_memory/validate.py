"""Neutral scalar validation shared by every domain module.

These three checks are the smallest thing two domains must agree on: what an argument of a given
shape is. They live outside the orchestrating application module so a domain module can reject an
argument by exactly the same rule as any other domain without importing the module that routes
dispatches — a domain that imported the application to borrow a validator would create a
dependency back edge, and a copy of the rule would be free to drift.
"""

from .domain import require


def exact(value, fields):
    """A mapping whose keys are exactly `fields`, no more and no fewer."""
    require(isinstance(value, dict) and set(value) == set(fields.split()), "invalid_input", 400)


def integer(value, minimum=0, maximum=2**31):
    """A real `int` inside the closed range. `bool` is not an integer here."""
    require(type(value) is int and minimum <= value <= maximum, "invalid_input", 400)


def string(value, maximum=2048):
    """A non-empty string no longer than `maximum` characters."""
    require(isinstance(value, str) and 0 < len(value) <= maximum, "invalid_input", 400)
