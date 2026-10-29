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


def test_unreferenced_source_default_is_independent_of_new_memory_retention():
    from types import SimpleNamespace

    from tianshu_memory.segment_records import expires

    service = SimpleNamespace(consolidation_ttl_days=dict(low=7, normal=30, high=180))
    memory = dict(kind="experience", state="described", retention="medium", valid_until=None)
    assert expires(service, "2026-10-30T00:00:00Z", memory) == "2026-11-06T00:00:00Z"
    assert expires(service, "2026-10-30T00:00:00Z", memory, kind="source") == "2026-11-29T00:00:00Z"
