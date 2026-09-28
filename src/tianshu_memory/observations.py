"""Verified passive source ledger; no dialogue, candidate, or profile side effects."""

import re
import ssl
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .domain import Fault, fingerprint, require, strict_json

QQ = re.compile(r"^[1-9][0-9]{0,19}$")
CONVERSATION = re.compile(r"^(group|private):[1-9][0-9]{0,19}$")
IDENT = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
HEX = re.compile(r"^[0-9a-f]{64}$")


class PlatformVerifier:
    def __init__(self, config_path):
        self.config_path = Path(config_path)

    def __call__(self, request):
        try:
            config = strict_json(self.config_path.read_bytes())["observation_source"]
            url, token = config["verify_url"], config["verify_token"]
            parsed = urlsplit(url)
            require(
                parsed.scheme == "https"
                and parsed.path == "/internal/v2/observation-source/verify"
                and parsed.hostname
                and not parsed.username
                and not parsed.password
                and not parsed.query
                and not parsed.fragment,
                "dependency_unavailable",
                503,
            )
            require(
                isinstance(token, str) and 24 <= len(token) <= 4096, "dependency_unavailable", 503
            )
            verify = (
                ssl.create_default_context(cafile=config["ca_file"])
                if config.get("ca_file")
                else True
            )
            with httpx.Client(
                verify=verify, timeout=5, follow_redirects=False, trust_env=False
            ) as client:
                response = client.post(
                    url,
                    json=request,
                    headers={"Authorization": "Bearer " + token, "Accept-Encoding": "identity"},
                )
            if response.status_code == 409 and len(response.content) <= 4096:
                denied = strict_json(response.content)
                if isinstance(denied, dict) and denied.get("code") == "scope_changed":
                    raise Fault("scope_changed", 409)
            require(
                response.status_code == 200
                and len(response.content) <= 4096
                and response.headers.get("content-type", "").split(";", 1)[0] == "application/json",
                "dependency_unavailable",
                503,
            )
            result = strict_json(response.content)
            require(
                isinstance(result, dict) and result.get("valid") is True,
                "dependency_unavailable",
                503,
            )
            return result
        except (OSError, ValueError, KeyError, TypeError, httpx.HTTPError, ssl.SSLError):
            raise Fault("dependency_unavailable", 503) from None


def _request(request):
    require(
        isinstance(request, dict)
        and set(request) == {"event", "source_ref", "source_digest", "scope_version"},
        "invalid_input",
        400,
    )
    event = request["event"]
    require(
        isinstance(event, dict)
        and set(event)
        == {
            "schema_version",
            "platform_id",
            "self_id",
            "namespace",
            "conversation_id",
            "account_id",
            "event_id",
            "revision",
            "sent_at",
            "text",
            "content_state",
            "mentioned",
            "scope_revision",
        },
        "invalid_input",
        400,
    )
    require(
        type(event["schema_version"]) is int
        and event["schema_version"] == 2
        and type(event["revision"]) is int
        and event["revision"] == 1
        and event["namespace"] == "qq"
        and isinstance(event["platform_id"], str)
        and IDENT.fullmatch(event["platform_id"])
        and isinstance(event["self_id"], str)
        and QQ.fullmatch(event["self_id"])
        and isinstance(event["conversation_id"], str)
        and CONVERSATION.fullmatch(event["conversation_id"])
        and isinstance(event["account_id"], str)
        and QQ.fullmatch(event["account_id"])
        and event["account_id"] != event["self_id"]
        and isinstance(event["event_id"], str)
        and IDENT.fullmatch(event["event_id"])
        and isinstance(event["text"], str)
        and len(event["text"]) <= 8000
        and event["content_state"] in {"text", "unsupported"}
        and type(event["mentioned"]) is bool
        and type(event["scope_revision"]) is int
        and event["scope_revision"] >= 1
        and isinstance(event["sent_at"], str)
        and len(event["sent_at"]) <= 40
        and isinstance(request["source_ref"], str)
        and IDENT.fullmatch(request["source_ref"])
        and isinstance(request["source_digest"], str)
        and HEX.fullmatch(request["source_digest"])
        and type(request["scope_version"]) is int
        and request["scope_version"] >= 1,
        "invalid_input",
        400,
    )
    try:
        moment = datetime.fromisoformat(event["sent_at"].replace("Z", "+00:00"))
        require(moment.tzinfo is not None, "invalid_input", 400)
    except ValueError:
        raise Fault("invalid_input", 400) from None
    require(fingerprint(event) == request["source_digest"], "invalid_input", 400)
    require(event["content_state"] != "unsupported" or event["text"] == "", "invalid_input", 400)
    if event["conversation_id"].startswith("private:"):
        require(
            event["conversation_id"][8:] == event["account_id"] and not event["mentioned"],
            "invalid_input",
            400,
        )
    return event


class ObservationLedger:
    def __init__(self, store, verifier):
        self.store, self.verifier = store, verifier

    def _ready(self, db):
        row = db.execute("SELECT value FROM metadata WHERE key='observation_schema'").fetchone()
        require(row is not None and row[0] == "1", "dependency_unavailable", 503)

    def ingest(self, service, request):
        require(service == "companion", "forbidden", 403)
        event = _request(request)
        proof = self.verifier(
            {
                "operation": "ingest",
                "source_ref": request["source_ref"],
                "source_digest": request["source_digest"],
                "scope_version": request["scope_version"],
            }
        )
        for wire, claimed in (
            ("instance_id", "platform_id"),
            ("self_id", "self_id"),
            ("conversation_id", "conversation_id"),
            ("account_id", "account_id"),
            ("event_id", "event_id"),
        ):
            require(proof.get(wire) == event[claimed], "forbidden", 403)
        require(
            proof.get("source_ref") == request["source_ref"]
            and proof.get("source_digest") == request["source_digest"]
            and proof.get("scope_version") == request["scope_version"]
            and type(proof.get("archive_epoch")) is int,
            "forbidden",
            403,
        )
        with self.store.transaction() as db:
            self._ready(db)
            old = db.execute(
                "SELECT digest FROM observation_sources WHERE source_ref=?",
                (request["source_ref"],),
            ).fetchone()
            if old:
                require(old["digest"] == request["source_digest"], "idempotency_conflict", 409)
                state = "duplicate"
            else:
                db.execute(
                    "INSERT INTO observation_sources VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        request["source_ref"],
                        request["source_digest"],
                        event["platform_id"],
                        event["self_id"],
                        event["conversation_id"],
                        event["account_id"],
                        event["event_id"],
                        request["scope_version"],
                        proof["archive_epoch"],
                        event["sent_at"],
                        event["content_state"],
                        event["text"],
                        int(event["mentioned"]),
                        "active",
                        time.time(),
                    ),
                )
                state = "accepted"
        return {"source_ref": request["source_ref"], "state": state, "archive_state": "archived"}

    def query(self, service, request):
        require(service == "companion", "forbidden", 403)
        require(
            isinstance(request, dict)
            and set(request) == {"instance_id", "self_id", "conversation_id", "limit", "cursor"},
            "invalid_input",
            400,
        )
        instance, account, conversation = (
            request["instance_id"],
            request["self_id"],
            request["conversation_id"],
        )
        require(
            isinstance(instance, str)
            and IDENT.fullmatch(instance)
            and isinstance(account, str)
            and QQ.fullmatch(account)
            and isinstance(conversation, str)
            and CONVERSATION.fullmatch(conversation)
            and type(request["limit"]) is int
            and 1 <= request["limit"] <= 100
            and (
                request["cursor"] is None
                or isinstance(request["cursor"], str)
                and IDENT.fullmatch(request["cursor"])
            ),
            "invalid_input",
            400,
        )
        proof = self.verifier(
            {
                "operation": "scope",
                "instance_id": instance,
                "self_id": account,
                "conversation_id": conversation,
            }
        )
        require(type(proof.get("archive_epoch")) is int, "forbidden", 403)
        with self.store.transaction() as db:
            self._ready(db)
            rows = db.execute(
                "SELECT source_ref,conversation,author,event_id,sent_at,content_state,"
                "text,mentioned,received_at FROM observation_sources "
                "WHERE instance_id=? AND self_id=? AND conversation=? "
                "AND archive_epoch=? AND state='active' "
                "AND (? IS NULL OR source_ref>?) ORDER BY source_ref LIMIT ?",
                (
                    instance,
                    account,
                    conversation,
                    proof["archive_epoch"],
                    request["cursor"],
                    request["cursor"],
                    request["limit"] + 1,
                ),
            ).fetchall()
        return {
            "items": [dict(row) for row in rows[: request["limit"]]],
            "next_cursor": rows[request["limit"] - 1]["source_ref"]
            if len(rows) > request["limit"]
            else None,
        }
