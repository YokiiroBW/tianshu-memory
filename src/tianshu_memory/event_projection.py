"""Conservative event slots: explicit qualified updates, never last-write-wins."""

from collections import defaultdict

from .domain import canonical


def project(claims):
    by_id = {claim["id"]: claim for claim in claims}
    rejected, replaced = set(), set()
    disputes = []
    for claim in claims:
        if claim["qualification"] == "third_party" or (
            claim["slot"] == "participant" and claim["speaker"] != claim["person_id"]
        ):
            rejected.add(claim["id"])
            disputes.append(dict(reason="unconfirmed_report", claim_ids=[claim["id"]]))
            continue
        if claim["relation"] not in {"updates", "cancels"}:
            continue
        for identity in claim["replaces"]:
            prior = by_id.get(identity)
            if (
                prior is not None
                and all(
                    prior[key] == claim[key]
                    for key in (
                        "event_id",
                        "slot",
                        "attribute",
                        "person_id",
                        "qualification",
                        "speaker",
                    )
                )
                and identity not in rejected
            ):
                replaced.add(identity)
    global_slots, participants = {}, defaultdict(dict)
    for claim in sorted(claims, key=lambda row: (row["recorded_at"], row["id"])):
        if claim["id"] in rejected:
            continue
        slots = global_slots if claim["slot"] == "global" else participants[claim["person_id"]]
        slot = slots.setdefault(claim["attribute"], dict(current=[], history=[]))
        slot["history" if claim["id"] in replaced else "current"].append(claim)
    for slots in [global_slots, *participants.values()]:
        for slot in slots.values():
            if len({canonical(claim["value"]) for claim in slot["current"]}) > 1:
                disputes.append(
                    dict(
                        reason="parallel_claims",
                        claim_ids=[claim["id"] for claim in slot["current"]],
                    )
                )
    return dict(global_slots=global_slots, participants=dict(participants), disputes=disputes)
