"""Server-owned retention decisions using structured fields only."""


def decide(memory):
    if memory["kind"] == "agreement" and memory["state"] in {"confirmed", "considering", "unknown"}:
        return "long", "high"
    if memory["kind"] == "thought" and memory.get("negations"):
        return "long", "high"
    if memory.get("valid_until"):
        return "medium", "normal"
    return "short", "normal"
