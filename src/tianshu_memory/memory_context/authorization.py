"""Current scope authorization from explicit private associations and bindings."""

import json

from ..domain import Fault, canonical
from ..postgres import is_postgres


def authorized_scopes(service, db, scope, include_associated):
    result = [(scope, None, 1)]
    if not include_associated or scope["audience"] != "self_private":
        return result
    if is_postgres(db):
        # JSONB object text is not canonical application JSON. Compare the
        # complete endpoint objects, without weakening their scope identity.
        read = db.native
        endpoint_match = "((scopes::jsonb #> '{0}')=?::jsonb OR (scopes::jsonb #> '{1}')=?::jsonb)"
    else:
        read = db.execute
        endpoint_match = "(json_extract(scopes,'$[0]')=? OR json_extract(scopes,'$[1]')=?)"
    rows = read(
        "SELECT * FROM context_associations WHERE actor_id=? AND state='linked' AND (source_person=? OR target_person=?) "
        "AND " + endpoint_match + " ORDER BY id LIMIT 17",
        (
            scope["actor_id"],
            scope["person_id"],
            scope["person_id"],
            canonical(scope),
            canonical(scope),
        ),
    ).fetchall()
    if len(rows) > 16:
        raise Fault("budget_exceeded", 429)
    for row in rows:
        scopes = json.loads(row["scopes"])
        if scope not in scopes:
            continue
        bindings = [
            service._binding(db, json.loads(row[key]))
            for key in ("source_account", "target_account")
        ]
        if any(
            binding is None
            or binding["person_id"] != row[person]
            or binding["version"] != row[version]
            for binding, person, version in zip(
                bindings,
                ("source_person", "target_person"),
                ("source_binding", "target_binding"),
                strict=True,
            )
        ):
            continue
        other = scopes[1] if scopes[0] == scope else scopes[0]
        if other not in [entry[0] for entry in result]:
            result.append((other, row["id"], row["version"]))
    return result
