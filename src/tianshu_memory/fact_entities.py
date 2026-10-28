"""Source-backed object identity within the existing automatic fact ledger.

Entity references are matching hints, never person IDs or grants. No separate
entity store is needed: active sourced facts own their objects and aliases.
"""

import json
import re
import unicodedata

from .recall_language import history_query, normalize_cups, normalize_objects, topic_aliases

OBJECT_NOUNS = {
    "notebook": ("本", ("笔记本", "笔记", "手账")),
    "book": ("本", ("书", "书籍")),
    "pen": ("支", ("钢笔", "圆珠笔", "铅笔", "笔")),
    "cat": ("只", ("猫", "猫咪")),
    "dog": ("只", ("狗", "狗狗")),
}


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
    mention = normalize_cups(normalize_mention(entity["mention"]))
    if entity["kind"] in {"notebook", "book", "journal", "diary"}:
        mention = normalize_objects(mention)
        if "封面" in mention:
            mention = re.sub(r"(?:这|那|的)?本|笔记本|笔记|手账|书籍|书", "", mention)
            mention = mention.replace("的", "")
    position = ordinal(mention)
    if position:
        mention = re.sub(r"第[一二三四五六七八九十百零两0-9]+", "第" + position, mention, count=1)
    return entity["kind"], mention


def fact_key(fact):
    entity = fact.get("entity")
    key = fact["key"] if entity is None else fact["key"] + "@" + entity["id"]
    modality = fact.get("canonical", {}).get("modality", "asserted")
    if modality != "asserted":
        key += "~" + modality
    return key + "~fictional" if fact.get("reality") == "fictional" else key


def filter_targets(db, candidates, query, *, facts=None, decoded=None):
    """Constrain explicitly named/numbered objects before ranking, within authorized rows."""
    if query.partition(":")[0] in {"field", "item"}:
        return candidates
    objects, by_group = {}, {}
    normalized = normalize_objects(normalize_mention(query))
    for group, _, _ in candidates:
        row = (
            db.execute(
                "SELECT payload FROM automatic_facts WHERE group_id=?", (group["id"],)
            ).fetchone()
            if facts is None
            else facts.get(group["id"])
        )
        fact = (
            (json.loads(row["payload"]) if decoded is None else decoded[group["id"]])
            if row
            else None
        )
        entity = fact.get("entity") if fact else None
        if entity:
            by_group[group["id"]] = entity
            objects.setdefault(entity["kind"], []).append(entity)
    wanted = {}
    number = r"第[一二三四五六七八九十百零两0-9]+(?:只|支|个|本|张|台|辆|件)?"
    for kind, entities in objects.items():
        direct = {e["id"] for e in entities if anchor(e)[1] in normalized}
        numbered = False
        for entity in entities:
            noun = re.sub(number, "", normalize_mention(entity["mention"]))
            label = noun or entity["kind"]
            aliases = {normalize_mention(alias) for alias in {label, *topic_aliases(label)}}
            numbered_list = number + "(?:(?:和|与|及|跟|、|/)" + number + ")*"
            pattern = (
                numbered_list
                + "(?:"
                + "|".join(re.escape(alias) for alias in sorted(aliases))
                + ")"
            )
            positions = {
                ordinal(position[0])
                for match in re.finditer(pattern, normalized)
                for position in re.finditer(number, match[0])
            }
            if positions:
                numbered = True
                if ordinal(entity["mention"]) in positions:
                    direct.add(entity["id"])
        # Bind each ordinal to its following noun; unrelated numbered objects must
        # not widen this target. Unknown positions deliberately select no object.
        if direct or numbered:
            wanted[kind] = direct
    return [
        entry
        for entry in candidates
        if (entity := by_group.get(entry[0]["id"])) is None
        or entity["kind"] not in wanted
        or entity["id"] in wanted[entity["kind"]]
    ]


def historical_title_groups(db, candidates, query, *, facts=None, decoded=None):
    """Link a quoted current/old title to sourced history of that same object/property."""
    titles = re.findall(r'[“「《"]([^”」》"]+)[”」》"]', query)
    if not titles or not history_query(query):
        return set()
    identities = {}
    targets = set()
    for group, _, _ in candidates:
        row = (
            db.execute(
                "SELECT payload FROM automatic_facts WHERE group_id=?", (group["id"],)
            ).fetchone()
            if facts is None
            else facts.get(group["id"])
        )
        fact = (
            (json.loads(row["payload"]) if decoded is None else decoded[group["id"]])
            if row
            else None
        )
        if not fact or not fact.get("entity"):
            continue
        key = (fact["entity"]["id"], fact["key"])
        identities[group["id"]] = key
        if any(title in fact["value"] for title in titles):
            targets.add(key)
    return {group for group, key in identities.items() if key in targets}


def historical_candidates(scope, query):
    """Index hints for the same object's property; current authorization is checked later."""
    titles = re.findall(r'[“「《"]([^”」》"]+)[”」》"]', query)
    if not titles or not history_query(query):
        return "", []
    matches = " OR ".join("instr(json_extract(t.payload,'$.value'),?)>0" for _ in titles)
    return (
        "SELECT f.group_id FROM automatic_facts f WHERE f.scope=? AND "
        "(json_extract(f.payload,'$.entity.id'),json_extract(f.payload,'$.key')) IN "
        "(SELECT json_extract(t.payload,'$.entity.id'),json_extract(t.payload,'$.key') "
        "FROM automatic_facts t WHERE t.scope=? AND (" + matches + "))",
        [scope, scope, *titles],
    )
