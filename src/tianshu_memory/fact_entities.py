"""Source-backed object identity within the existing automatic fact ledger.

Entity references are matching hints, never person IDs or grants. No separate
entity store is needed: active sourced facts own their objects and aliases.
"""

import json
import re
import unicodedata

from .domain import fingerprint, require


def normalize_mention(value):
    value = unicodedata.normalize("NFKC", value).casefold()
    value = re.sub(r"^(?:我的|我家(?:的)?|我养的|我买的|本人的|my\s+)", "", value)
    return re.sub(r"\s+", "", value).strip('，。！？,.!?"“”')


def ordinal(value):
    match = re.search(r"第([一二三四五六七八九十百零两0-9]+)(?:只|支|个|本|张|台|辆|件)?", value)
    if not match:
        return None
    number = match[1]
    small = {ch: str(i) for i, ch in enumerate("零一二三四五六七八九")}
    return small.get(number, number)


def anchor(entity):
    mention = normalize_mention(entity["mention"])
    position = ordinal(mention)
    if position:
        mention = re.sub(r"第[一二三四五六七八九十百零两0-9]+", "第" + position, mention, count=1)
    return entity["kind"], mention


def compatible(left, right):
    if left == right:
        return True
    if left[0] != right[0] or not ordinal(left[1]) or ordinal(left[1]) != ordinal(right[1]):
        return False
    # A short generic noun can abbreviate a uniquely identified numbered object.
    # Preserve richer labels: first fountain pen and first ballpoint are different.
    nouns = [
        re.sub(r"第[0-9]+(?:只|支|个|本|张|台|辆|件)?", "", label) for _, label in (left, right)
    ]
    return any(len(short) == 1 and long.endswith(short) for short, long in (nouns, nouns[::-1]))


def validate(entity):
    if entity is None:
        return
    require(
        type(entity) is dict and set(entity) == {"id", "kind", "mention"},
        "invalid_input",
        400,
    )
    require(
        entity["id"] is None
        or (type(entity["id"]) is str and re.fullmatch(r"entity:[0-9a-f]{64}", entity["id"])),
        "invalid_input",
        400,
    )
    require(
        type(entity["kind"]) is str and re.fullmatch(r"[a-z][a-z0-9_.-]{0,39}", entity["kind"]),
        "invalid_input",
        400,
    )
    require(
        type(entity["mention"]) is str and 0 < len(entity["mention"].strip()) <= 100,
        "invalid_input",
        400,
    )


def resolve(service, db, scope, fact, quotes):
    entity = fact.get("entity")
    if entity is None:
        return fact
    require(any(entity["mention"] in q["quote"] for q in quotes), "invalid_input", 400)
    objects = {}
    for row in db.execute("SELECT group_id,payload FROM automatic_facts WHERE scope=?", (scope,)):
        prior = json.loads(row["payload"]).get("entity")
        if (
            prior
            and compatible(anchor(entity), anchor(prior))
            and service._group_current(db, row["group_id"])
        ):
            objects.setdefault(prior["id"], set()).add(anchor(prior))
    matches = {
        identity
        for identity, labels in objects.items()
        if any(compatible(anchor(entity), label) for label in labels)
    }
    # A reference cannot silently rename another object or bind a foreign/revoked object.
    # The model must quote a distinguishing source mention, including for corrections.
    if entity["id"] is not None:
        require(entity["id"] in matches and len(matches) == 1, "invalid_input", 400)
        identity = entity["id"]
    else:
        require(len(matches) <= 1, "invalid_input", 400)
        identity = next(iter(matches), None)
        if identity is None:
            identity = "entity:" + fingerprint([scope, *anchor(entity)])
    return dict(fact, entity=dict(entity, id=identity))


def fact_key(fact):
    entity = fact.get("entity")
    return fact["key"] if entity is None else fact["key"] + "@" + entity["id"]


def filter_targets(db, candidates, query):
    """Constrain explicitly named/numbered objects before ranking, within authorized rows."""
    if query.partition(":")[0] in {"field", "item"}:
        return candidates
    objects, by_group = {}, {}
    normalized = normalize_mention(query)
    for group, _, _ in candidates:
        row = db.execute(
            "SELECT payload FROM automatic_facts WHERE group_id=?", (group["id"],)
        ).fetchone()
        entity = json.loads(row[0]).get("entity") if row else None
        if entity:
            by_group[group["id"]] = entity
            objects.setdefault(entity["kind"], []).append(entity)
    wanted = {}
    query_ordinals = {
        ordinal(match[0])
        for match in re.finditer(
            r"第[一二三四五六七八九十百零两0-9]+(?:只|支|个|本|张|台|辆|件)?", query
        )
    }
    for kind, entities in objects.items():
        direct = {e["id"] for e in entities if normalize_mention(e["mention"]) in normalized}
        if direct:
            wanted[kind] = direct
            continue
        # A numbered request must not fall back to another numbered item of the same kind.
        nouns = {
            re.sub(
                r"第[一二三四五六七八九十百零两0-9]+(?:只|支|个|本|张|台|辆|件)?",
                "",
                normalize_mention(e["mention"]),
            )
            for e in entities
        }
        if query_ordinals and any(noun and noun in normalized for noun in nouns):
            wanted[kind] = {e["id"] for e in entities if ordinal(e["mention"]) in query_ordinals}
    return [
        entry
        for entry in candidates
        if (entity := by_group.get(entry[0]["id"])) is None
        or entity["kind"] not in wanted
        or entity["id"] in wanted[entity["kind"]]
    ]
