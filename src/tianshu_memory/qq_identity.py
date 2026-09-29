"""QQ account shape and read-only platform-owned profile projection."""

import json
import re

from .domain import canonical, fingerprint, parse_time, require


def qq_id(value):
    if type(value) is int:
        require(value > 0, "invalid_input", 400)
        return str(value)
    require(type(value) is str and re.fullmatch(r"[1-9][0-9]*", value), "invalid_input", 400)
    return value


def validate_account(account):
    require(
        type(account) is dict and set(account) == {"namespace", "immutable_account_id"},
        "invalid_input",
        400,
    )
    if account["namespace"] == "qq":
        require(
            account["immutable_account_id"] == qq_id(account["immutable_account_id"]),
            "invalid_input",
            400,
        )


def _display(value):
    if value is None:
        return None
    require(type(value) is str and 1 <= len(value.strip()) <= 80, "invalid_input", 400)
    require(
        all(
            ord(c) >= 32
            and ord(c) != 127
            and not 0x202A <= ord(c) <= 0x202E
            and not 0x2066 <= ord(c) <= 0x2069
            for c in value
        ),
        "invalid_input",
        400,
    )
    return value.strip()


def observe_alias(store, payload):
    require(
        type(payload) is dict
        and set(payload)
        == {
            "schema_version",
            "request_id",
            "account_id",
            "bot_id",
            "conversation_id",
            "nickname",
            "group_card",
            "event_ref",
            "observed_at",
        },
        "invalid_input",
        400,
    )
    require(
        payload["schema_version"] == 1
        and type(payload["request_id"]) is str
        and 1 <= len(payload["request_id"]) <= 128,
        "invalid_input",
        400,
    )
    account_id, bot_id = qq_id(payload["account_id"]), qq_id(payload["bot_id"])
    conversation = (
        payload["conversation_id"].split(":", 1) if type(payload["conversation_id"]) is str else []
    )
    require(
        len(conversation) == 2 and conversation[0] in {"group", "private"}, "invalid_input", 400
    )
    qq_id(conversation[1])
    require(conversation[0] != "private" or conversation[1] == account_id, "invalid_input", 400)
    nickname, card = _display(payload["nickname"]), _display(payload["group_card"])
    require(conversation[0] == "group" or card is None, "invalid_input", 400)
    require(nickname is not None or card is not None, "invalid_input", 400)
    ref = payload["event_ref"]
    require(type(ref) is str and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", ref), "invalid_input", 400)
    require(
        type(payload["observed_at"]) is str and payload["observed_at"].endswith("Z"),
        "invalid_input",
        400,
    )
    require(parse_time(payload["observed_at"]).tzinfo is not None, "invalid_input", 400)
    digest = fingerprint({key: value for key, value in payload.items() if key != "request_id"})
    account_key = canonical({"namespace": "qq", "immutable_account_id": account_id})
    with store.transaction() as db:
        marker = db.execute("SELECT value FROM metadata WHERE key='qq_alias_schema'").fetchone()
        require(marker is not None and marker[0] == "1", "dependency_unavailable", 503)
        require(
            db.execute("SELECT 1 FROM accounts WHERE account_key=?", (account_key,)).fetchone(),
            "not_found",
            404,
        )
        previous = db.execute(
            "SELECT digest FROM qq_alias_events WHERE event_ref=?", (ref,)
        ).fetchone()
        if previous:
            require(previous["digest"] == digest, "idempotency_conflict", 409)
            return {"schema_version": 1, "request_id": payload["request_id"], "deduplicated": True}
        for kind, value, group in (
            ("nickname", nickname, ""),
            ("group_card", card, conversation[1]),
        ):
            if value is None:
                continue
            row = db.execute(
                "SELECT observed_at FROM qq_aliases WHERE account_key=? AND kind=? AND bot_id=? AND group_id=?",
                (account_key, kind, bot_id, group),
            ).fetchone()
            if row is None or parse_time(row["observed_at"]) <= parse_time(payload["observed_at"]):
                db.execute(
                    "INSERT INTO qq_aliases VALUES (?,?,?,?,?,?,?) ON CONFLICT(account_key,kind,bot_id,group_id) DO UPDATE SET value=excluded.value,observed_at=excluded.observed_at,event_ref=excluded.event_ref",
                    (account_key, kind, bot_id, group, value, payload["observed_at"], ref),
                )
        db.execute("INSERT INTO qq_alias_events VALUES (?,?)", (ref, digest))
    return {"schema_version": 1, "request_id": payload["request_id"], "deduplicated": False}


def profiles(store, *, limit, after):
    require(type(limit) is int and 1 <= limit <= 100, "invalid_input", 400)
    require(after is None or type(after) is str, "invalid_input", 400)
    if after is not None:
        qq_id(after)
    after_key = canonical({"namespace": "qq", "immutable_account_id": after}) if after else ""
    with store.transaction() as db:
        rows = db.execute(
            "SELECT account_key,person_id,display_name FROM accounts "
            "WHERE account_key>? AND json_extract(account_key,'$.namespace')='qq' "
            "ORDER BY account_key LIMIT ?",
            (after_key, limit + 1),
        ).fetchall()
        marker = db.execute("SELECT value FROM metadata WHERE key='qq_alias_schema'").fetchone()
        aliases = (
            db.execute(
                "SELECT account_key,kind,bot_id,group_id,value,observed_at FROM qq_aliases WHERE account_key IN ("
                + ",".join("?" for _ in rows)
                + ")",
                tuple(row["account_key"] for row in rows),
            ).fetchall()
            if rows and marker is not None and marker[0] == "1"
            else []
        )
    items = []
    for row in rows:
        key = json.loads(row["account_key"])
        if key.get("namespace") != "qq":
            continue
        account_id = qq_id(key["immutable_account_id"])
        if after is not None and account_id <= after:
            continue
        names = [
            dict(
                kind=a["kind"],
                bot_id=a["bot_id"],
                group_id=a["group_id"],
                value=a["value"],
                observed_at=a["observed_at"],
            )
            for a in aliases
            if a["account_key"] == row["account_key"]
        ]
        global_names = sorted(
            (name for name in names if name["kind"] == "nickname"),
            key=lambda name: parse_time(name["observed_at"]),
            reverse=True,
        )
        items.append(
            {
                "qq_id": account_id,
                "person_id": row["person_id"],
                "display_name": row["display_name"]
                or (global_names[0]["value"] if global_names else "用户 " + row["person_id"][-8:]),
                "aliases": names,
            }
        )
    items.sort(key=lambda item: item["qq_id"])
    next_cursor = items[limit - 1]["qq_id"] if len(items) > limit else None
    return {"schema_version": 1, "items": items[:limit], "next_cursor": next_cursor}
