"""Source-backed object identity within the existing automatic fact ledger.

Entity references are matching hints, never person IDs or grants. No separate
entity store is needed: active sourced facts own their objects and aliases.
"""

import json
import re
import unicodedata

from .domain import fingerprint, parse_time, require
from .recall_language import history_query, normalize_cups, normalize_objects, topic_aliases

OBJECT_NOUNS = {
    "notebook": ("本", ("笔记本", "笔记", "手账")),
    "book": ("本", ("书", "书籍")),
    "pen": ("支", ("钢笔", "圆珠笔", "铅笔", "笔")),
    "cat": ("只", ("猫", "猫咪")),
    "dog": ("只", ("狗", "狗狗")),
}


def grounded_ordinal(entity, quotes):
    """Expand only a numbered object whose noun is explicitly in the same quote."""
    classifier, nouns = OBJECT_NOUNS.get(entity["kind"], (None, ()))
    if not classifier or not entity["mention"].endswith(classifier):
        return None
    found = set()
    other_nouns = set()
    for proof in quotes:
        text = proof["quote"]
        if entity["mention"] not in text:
            continue
        for other_kind, (other_classifier, aliases) in OBJECT_NOUNS.items():
            if other_classifier == classifier and other_kind != entity["kind"]:
                for noun in aliases:
                    if re.search(
                        r"[两二三四五六七八九十0-9]+"
                        + classifier
                        + re.escape(noun)
                        + r"(?=[，,。；;：:\s]|$)",
                        text,
                    ):
                        other_nouns.add(noun)
        for noun in nouns:
            if re.search(
                r"[两二三四五六七八九十0-9]+"
                + classifier
                + re.escape(noun)
                + r"(?=[，,。；;：:\s]|$)",
                text,
            ):
                found.add(noun)
            # In '第一本笔记本叫…，第二本叫…', the explicitly named
            # sibling supplies the noun omitted from the later list item.
            if re.search(
                r"第[一二三四五六七八九十0-9]+"
                + classifier
                + re.escape(noun)
                + r"(?=叫|名|标题|[，,。；;：:])",
                text,
            ):
                found.add(noun)
    if len(found) == 1 and not other_nouns:
        return dict(entity, mention=entity["mention"] + next(iter(found)))
    return None


def normalize_mention(value):
    value = unicodedata.normalize("NFKC", value).casefold()
    value = re.sub(r"^(?:我的|我家(?:的)?|我养的|我买的|本人的|my\s+)", "", value)
    return re.sub(r"\s+", "", value).strip('，。！？,.!?"“”')


def reference_anchor(value):
    """Compare sourced classifier descriptions without changing stored identities.

    The optional attributive particle in “有银夹的那支/有银夹那支” is not a new
    object. This is deliberately narrower than stripping 的 from names or text.
    Callers still require a unique match among authorized same-type subjects.
    """
    return re.sub(r"的(?=那(?:只|支|个|本|张|台|辆|件)$)", "", normalize_mention(value))


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


def self_reference(entity, quotes):
    """A leading, explicit self-reference may abbreviate an existing object.

    The returned label is only a matching cue. Exact source evidence, current
    owner scope, unique entity and supplied-ID checks still decide the binding.
    """
    if not any(q["quote"].lstrip().startswith(entity["mention"]) for q in quotes):
        return None
    value = re.sub(r"\s+", "", unicodedata.normalize("NFKC", entity["mention"]))
    match = re.fullmatch(
        r"(?:我(?:的)?|我家(?:的)?|本人(?:的)?)(?:这|那)(?:只|支|个|本|张|台|辆|件)?(.+)",
        value,
    )
    return anchor(dict(entity, mention=match[1])) if match else None


def grounded_color(entity, quotes):
    """A neighboring named cup may ground a literal color-only list subject."""
    color = re.fullmatch(r"([红蓝绿黑白黄紫棕灰橙])色?的", normalize_mention(entity["mention"]))
    if entity["kind"] != "cup" or not color:
        return None
    cup = (
        r"(?:我的|我家(?:的)?|本人的)?"
        r"(?:第[一二三四五六七八九十百零两0-9]+(?:只|个)?)?"
        r"[红蓝绿黑白黄紫棕灰橙](?:色)?(?:的)?"
        r"(?:玻璃|陶瓷|不锈钢|塑料)?(?:水杯|杯子)"
    )
    naming = (
        r"(?:(?:现在|目前|后来)?(?:改叫|叫|名叫|叫作|名字是|名称是)|还是|仍(?:然)?叫|依(?:然|旧)叫)"
    )
    value = r"[^，,。！？!?；;：:\r\n]{1,80}"
    pattern = cup + naming + value + r"[，,]\s*" + re.escape(entity["mention"]) + naming + value
    if any(re.fullmatch(pattern + r"[。.]?", proof["quote"].strip()) for proof in quotes):
        return color[1]
    return None


def color_matches(entity, color):
    """Return possible and grounded matches without guessing an unfamiliar descriptor."""
    if entity["kind"] != "cup":
        return False, False
    label = normalize_mention(entity["mention"])
    noun = re.search(r"(?:水杯|杯子)$", label)
    if noun is None:
        return False, False
    descriptor = label[: noun.start()]
    if re.search(r"水杯|杯子|[，,。！？!?；;：:\"“”「」《》]", descriptor):
        return False, False
    # Explicit 色 also covers an unfamiliar material/finish or qualifier order;
    # excluding it would make two same-color cups look falsely unique.
    grounded = bool(
        re.search(re.escape(color) + r"色", descriptor)
        or re.search(re.escape(color) + r"的?(?:玻璃|陶瓷|不锈钢|塑料)?$", descriptor)
    )
    # An unknown bare descriptor can hide a second same-color object. Count it
    # as a possible conflict, but '红茶杯子' alone cannot ground the color red.
    return color in descriptor, grounded


def compatible(left, right):
    if left == right:
        return True
    if left[0] == right[0] and "封面" in left[1] and "封面" in right[1]:
        positions = [ordinal(label) for _, label in (left, right)]
        labels = [re.sub(r"第[0-9一二三四五六七八九十]+", "", label) for _, label in (left, right)]
        if labels[0] == labels[1] and (not all(positions) or positions[0] == positions[1]):
            return True
    if left[0] != right[0] or not ordinal(left[1]) or ordinal(left[1]) != ordinal(right[1]):
        return False
    # A short generic noun can abbreviate a uniquely identified numbered object.
    # Preserve richer labels: first fountain pen and first ballpoint are different.
    nouns = [
        re.sub(r"第[0-9]+(?:只|支|个|本|张|台|辆|件)?", "", label) for _, label in (left, right)
    ]
    if not all(nouns):
        return True
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


def _literal_alias(value):
    # Names are literal data: do not strip ownership words, punctuation or
    # quoted content as descriptor normalization deliberately does.
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _sourced_aliases(service, db, scope, candidates):
    """Resolve only exact name/title candidates through existing whole groups."""
    if not candidates:
        return set()
    from .fact_labels import positive_source
    from .postgres import GROUP_READ_BATCH
    from .recall_index import eligible_groups

    keys = sorted(candidates)
    matches, owner = set(), json.loads(scope)
    for offset in range(0, len(keys), GROUP_READ_BATCH):
        batch = keys[offset : offset + GROUP_READ_BATCH]
        groups = db.execute(
            "SELECT g.*,f.fact_key AS alias_key FROM groups g JOIN automatic_facts f "
            "ON f.group_id=g.id WHERE g.id IN ("
            + ",".join("?" for _ in batch)
            + ") AND f.superseded_by IS NULL",
            batch,
        ).fetchall()
        approved = {
            group["id"]: candidates[group["id"]]
            for group, _, units in eligible_groups(service, db, groups, owner)
            if len(units) == 1
            and not units[0]["conditions"]
            and not units[0]["negations"]
            and group["alias_key"] == fact_key(candidates[group["id"]])
        }
        if not approved:
            continue
        quotes = {identity: [] for identity in approved}
        for row in db.execute(
            "SELECT e.group_id,e.quote FROM automatic_evidence e JOIN lineage l "
            "ON l.group_id=e.group_id AND l.source_key=e.source_key WHERE e.group_id IN ("
            + ",".join("?" for _ in approved)
            + ")",
            sorted(approved),
        ):
            quotes[row["group_id"]].append(row["quote"])
        for identity, fact in approved.items():
            if positive_source(fact, quotes[identity]):
                matches.add(fact["entity"]["id"])
    return matches


def resolve(service, db, scope, fact, quotes):
    entity = fact.get("entity")
    if entity is None:
        return fact
    require(any(entity["mention"] in q["quote"] for q in quotes), "invalid_input", 400)
    abbreviated = re.fullmatch(
        r"第[一二三四五六七八九十百零两0-9]+(?:只|支|个|本|张|台|辆|件)?",
        normalize_mention(entity["mention"]),
    )
    requested = anchor(entity)
    reference = self_reference(entity, quotes)
    elliptical = re.fullmatch(r"[红蓝绿黑白黄紫棕灰橙]色?的", normalize_mention(entity["mention"]))
    color = grounded_color(entity, quotes) if elliptical else None
    require(not elliptical or color is not None, "invalid_input", 400)

    def matches_label(prior):
        label = anchor(prior)
        return compatible(requested, label) or bool(
            reference and reference[0] == label[0] and label[1].endswith(reference[1])
        )

    from .fact_labels import candidate as label_candidate

    matches, grounded, aliases = set(), set(), {}
    for row in db.execute("SELECT group_id,payload FROM automatic_facts WHERE scope=?", (scope,)):
        previous = json.loads(row["payload"])
        prior = previous.get("entity")
        if not prior:
            continue
        possible, supported = (
            color_matches(prior, color) if elliptical else (matches_label(prior), True)
        )
        if possible and service._group_current(db, row["group_id"]):
            matches.add(prior["id"])
            if supported:
                grounded.add(prior["id"])
        if (
            not elliptical
            and prior["kind"] == entity["kind"]
            and _literal_alias(previous["value"]) == _literal_alias(entity["mention"])
            and label_candidate(previous)
            and previous["state"] == "current"
        ):
            now = service.clock()
            if (not previous["valid_from"] or parse_time(previous["valid_from"]) <= now) and (
                not previous["valid_to"] or parse_time(previous["valid_to"]) > now
            ):
                aliases[row["group_id"]] = previous
    # A supplied ID cannot choose among ambiguous descriptions or names. An
    # alias reuses the same ledger identity, never a parallel named object.
    aliases = {key: value for key, value in aliases.items() if value["entity"]["id"] not in matches}
    matches.update(_sourced_aliases(service, db, scope, aliases))
    # A grounded color ellipse may reuse an object, never create a nounless one.
    require(not elliptical or (len(matches) == 1 and matches <= grounded), "invalid_input", 400)
    # A reference cannot silently rename another object or bind a foreign/revoked object.
    # The model must quote a distinguishing source mention, including for corrections.
    if entity["id"] is not None:
        require(entity["id"] in matches and len(matches) == 1, "invalid_input", 400)
        identity = entity["id"]
    else:
        require(len(matches) <= 1, "invalid_input", 400)
        identity = next(iter(matches), None)
        if identity is None:
            if abbreviated:
                entity = grounded_ordinal(entity, quotes)
                require(entity is not None, "invalid_input", 400)
            identity = "entity:" + fingerprint([scope, *anchor(entity)])
    return dict(fact, entity=dict(entity, id=identity))


def property_identity(fact):
    """Business property identity, independent of its assertion modality slot."""
    entity = fact.get("entity")
    subject = fact.get("canonical", {}).get("subject")
    return (
        subject["kind"] if subject else ("entity" if entity else "author"),
        entity["id"] if entity else None,
        fact["key"],
        fact.get("reality", "real"),
    )


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
