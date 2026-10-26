"""Exact source identity and the persisted audience constraint of an actor fact."""

from ..domain import fingerprint

INTERNAL = {"purpose": "management", "audience": {"kind": "actor_only"}}


def identity(actor, source, perspective):
    return fingerprint({"actor": actor, "source": source, "perspective": perspective})


def version_tuple(row):
    return tuple(row[key] for key in ("source_generation", "source_sequence", "access_version"))


def source_refs(fact):
    return [fact["source"], *(entry["content_ref"] for entry in fact["content_reads"])]


def permitted(fact, context):
    restriction, audience = fact["source_constraints"], context["audience"]
    if audience["kind"] == "actor_only":
        return True
    if restriction["kind"] == "public":
        return True
    return restriction["kind"] == audience["kind"] and all(
        audience.get(key) == value for key, value in restriction.items()
    )
