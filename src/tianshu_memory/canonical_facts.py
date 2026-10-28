"""Atomic sourced properties, explicit updates and stable object/event identity."""

from . import fact_names
from .domain import parse_time

MODALITIES = {
    "asserted": "陈述",
    "observed": "来源报告已发生",
    "planned": "计划（不代表已执行）",
    "conditional": "有条件",
    "uncertain": "未确定",
}


def render(value, *, operation=None):
    if value["predicate"] == fact_names.KEY and value["subject"]["kind"] == "author":
        text = "姓名：" + value["value"]
    else:
        subject = value["subject"]["mention"] or "用户"
        text = f"{subject} 的 {value['predicate']}：{value['value']}"
    if operation == "retract":
        text = "已撤回这项主张：" + text
    if value["modality"] != "asserted":
        text = MODALITIES[value["modality"]] + "；" + text
    return text


def current_groups(candidates, snapshot, clock):
    """Canonical current slots take precedence without rewriting legacy records."""
    from .fact_entities import fact_key

    if not any(fact.get("canonical") for fact in snapshot.decoded.values()):
        return candidates
    latest = {}
    for group, _, units in candidates:
        identity = group["id"]
        fact, row = snapshot.decoded.get(identity), snapshot.rows.get(identity)
        if (
            not fact
            or not fact.get("canonical")
            or fact["canonical"]["cardinality"] != "single"
            or fact["state"] != "current"
            or row["superseded_by"]
        ):
            continue
        effective = parse_time(fact["valid_from"] or row["observed_at"])
        if effective <= clock() and (
            not fact["valid_to"] or parse_time(fact["valid_to"]) > clock()
        ):
            key = (group["scope"], fact_key(fact))
            latest[key] = max(latest.get(key, (effective, "")), (effective, identity))
    result = []
    for entry in candidates:
        group, _, units = entry
        identity = group["id"]
        fact, row = snapshot.decoded.get(identity), snapshot.rows.get(identity)
        if not fact:
            result.append(entry)
            continue
        key = (group["scope"], fact_key(dict(fact, reality=units[0].get("reality", "real"))))
        chosen = latest.get(key)
        if chosen is not None and chosen[1] != identity:
            if fact.get("canonical"):
                if fact["canonical"]["cardinality"] == "single":
                    continue
            elif chosen[0] >= parse_time(fact["valid_from"] or row["observed_at"]):
                continue
        result.append(entry)
    return result
