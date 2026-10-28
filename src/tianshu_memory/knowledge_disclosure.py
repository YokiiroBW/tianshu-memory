"""Original-owner disclosure proof, independent of historical read access versions."""

import json

from .domain import canonical, fingerprint


def prove(application, db, request, authority):
    row = application._document(db, request["content_ref"], authority)
    evidence = json.loads(row["provenance"]).get("disclosure", {})
    public = (
        evidence.get("format") == "anonymous-public-fetch/v1"
        and evidence.get("public_anonymous") is True
        and bool(evidence.get("chain"))
        and all(
            hop.get("public_anonymous") is True
            and hop.get("anonymous") is True
            and hop.get("method") == "GET"
            for hop in evidence["chain"]
        )
    )
    audience, grant = request["audience"], None
    allowed = public
    if audience["kind"] == "user_scope":
        scope = audience["scope"]
        owner = json.loads(row["owner"])
        if owner == {"kind": "user", "scope": scope}:
            allowed = True
        else:
            found = db.execute(
                "SELECT * FROM knowledge_content_grants WHERE document_id=? AND reader=?",
                (row["id"], canonical(scope)),
            ).fetchone()
            grant = dict(found) if found else None
            allowed = bool(
                grant
                and grant["enabled"] == 1
                and grant["version"] == row["version"]
                and grant["hash"] == row["hash"]
            )
    return dict(
        read_allowed=True,
        disclosure_allowed=allowed,
        classification="public_anonymous" if public else "private",
        disclosure_version=fingerprint(
            dict(
                reference=request["content_ref"],
                evidence=evidence,
                access_version=row["access_version"],
                audience=audience,
                grant=grant,
            )
        ),
    )
