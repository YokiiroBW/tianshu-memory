import pytest

from tianshu_memory.retention import decide


@pytest.mark.parametrize(
    "memory,expected",
    [
        (
            dict(kind="agreement", state="confirmed", retention="short", importance="low"),
            ("long", "high"),
        ),
        (dict(kind="agreement", state="considering"), ("long", "high")),
        (dict(kind="thought", state="described", negations=["明确边界"]), ("long", "high")),
        (
            dict(kind="experience", state="described", valid_until="2026-11-01T00:00:00Z"),
            ("medium", "normal"),
        ),
        (
            dict(kind="experience", state="described", retention="long", importance="high"),
            ("short", "normal"),
        ),
        (dict(kind="agreement", state="completed"), ("short", "normal")),
    ],
)
def test_rules_override_model_choices_using_structured_fields(memory, expected):
    assert decide(memory) == expected
