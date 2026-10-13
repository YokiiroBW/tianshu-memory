"""Curated synthetic recall regression corpus, scored independently of LLM answers."""

import json
from pathlib import Path

import pytest

DATA = json.loads(
    (Path(__file__).parent / "fixtures/recall_quality.json").read_text(encoding="utf-8")
)


@pytest.mark.parametrize("case", DATA["queries"], ids=lambda case: case["id"])
def test_recall_quality(h, case):
    h.seed(
        [
            h.draft(
                field_key=fact["id"],
                units=[h.unit(statement=fact["text"], conditions=[], negations=[])],
            )
            for fact in DATA["facts"]
        ]
    )
    result = h.select(query=case["query"])
    expected = {fact["text"] for fact in DATA["facts"] if fact["id"] in case["expected"]}
    assert {u["statement"] for u in result["selected_units"]} == expected
