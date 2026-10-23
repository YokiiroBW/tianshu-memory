"""Multiple textual owner targets inside the already-authorized candidate domain."""

import json
import re

from ..automatic_facts import normalize
from ..recall_language import NamedReferenceQuery, self_reference, topic_tokens


def resolve(service, snapshot, candidates, query, requester, *, complete):
    by_group = {entry[0]["id"]: entry for entry in candidates}
    names = snapshot.names
    current = names.current(service.clock, normalize) if names else set()
    values = {}
    for identity in current:
        entry = names.entries[identity]
        values.setdefault(entry.fact["value"].strip(), []).append(by_group[identity])
    targets, witnesses = [], []
    matched_spans = []
    # Resolve known literal names before segmenting conjunctions: a person's
    # actual quoted/literal name may itself contain a conjunction.
    for name in sorted(values, key=len, reverse=True):
        pattern = (
            re.escape(name)
            + r"(?=\s*(?:的|家(?:的)?|那(?:一)?只|这(?:一)?只|['’]s\b|(?:之前|以前|上次|刚才)?(?:提到|说起|说)过))"
        )
        matches = list(re.finditer(pattern, query, re.I))
        for match in matches:
            if any(start <= match.start() < end for start, end in matched_spans):
                continue
            if (
                match.start()
                and query[match.start() - 1].isalnum()
                and query[match.start() - 1] not in "和与及跟"
            ):
                # The original bounded grammar handles operation/preamble text.
                if NamedReferenceQuery(query).for_name(name) is None:
                    continue
            matched_spans.append(match.span())
            owners = {entry[0]["scope"] for entry in values[name]}
            known = complete and len(owners) == 1 and not names.incomplete
            chosen = values[name][0] if known else None
            target = dict(
                target_id="target:" + str(len(targets)),
                kind="named",
                mention=name,
                status="resolved" if known else "ambiguous",
                person_id=json.loads(chosen[0]["scope"])["person_id"] if chosen else None,
                witness_group_ids=[chosen[0]["id"]] if chosen else [],
            )
            targets.append(target)
            if chosen:
                witnesses.append(chosen)
    if self_reference(query) or (
        not targets and query.lstrip().startswith("我") and "群里" not in query
    ):
        targets.insert(
            0,
            dict(
                target_id="target:self",
                kind="self",
                mention="我",
                status="resolved",
                person_id=requester["person_id"],
                witness_group_ids=[],
            ),
        )
    object_mentions = [
        fact.get("entity", {}).get("mention", "")
        for fact in snapshot.decoded.values()
        if fact.get("entity")
    ]
    for part in re.split(r"[，,；;]|(?:和|与|及|跟)", query):
        if self_reference(part) or any(
            NamedReferenceQuery(part).for_name(name) is not None for name in values
        ):
            continue
        explicit = NamedReferenceQuery(part).explicit()
        plain = re.match(r"\s*([\u3400-\u9fffA-Za-z][\w\u3400-\u9fff-]{0,20}?)的", part)
        mention = plain[1] if plain else part.strip()
        if any(target["kind"] == "named" and target["mention"] == mention for target in targets):
            continue
        object_target = any(
            value and value.replace("的", "") in part.replace("的", "") for value in object_mentions
        )
        if not explicit and (plain is None or topic_tokens(mention) or object_target):
            continue
        if mention in {"我", "本人", "群里", "之前", "以前", "上次", "那次"}:
            continue
        targets.append(
            dict(
                target_id="target:" + str(len(targets)),
                kind="named",
                mention=mention,
                status="unknown",
                person_id=None,
                witness_group_ids=[],
            )
        )
    if not targets and requester["audience"] == "self_private":
        targets.append(
            dict(
                target_id="target:self",
                kind="self",
                mention="本人",
                status="resolved",
                person_id=requester["person_id"],
                witness_group_ids=[],
            )
        )
    # Repeated mentions share one semantic target, preserving each unique spelling.
    unique = {(target["kind"], target["mention"]): target for target in targets}
    targets = list(unique.values())[:8]
    if not targets:
        mode = "room"
    elif len(targets) > 1:
        mode = "multiple"
    else:
        mode = targets[0]["kind"] if targets[0]["status"] == "resolved" else "unresolved"
    return dict(mode=mode, targets=targets), list(
        {entry[0]["id"]: entry for entry in witnesses}.values()
    )
