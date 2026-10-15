"""English remains supported alongside the Chinese social regression corpus."""

from tianshu_memory.fact_updates import may_replace


def test_english_denial_matches_only_its_named_current_value():
    prior = dict(key="beverage.preference", value="Oat Milk")
    unit = dict(statement="I usually drink oat milk", conditions=[], negations=[])
    for statement, expected in [
        ("I don't drink tea anymore", False),
        ("I no longer drink oat milk", True),
        ("I do not drink Oat Milk anymore.", True),
    ]:
        assert (
            may_replace(prior, unit, dict(prior, value=statement), dict(unit, statement=statement))
            is expected
        )
