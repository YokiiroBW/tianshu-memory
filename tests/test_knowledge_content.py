"""Actual bytes/codecs through published service routes in isolated synthetic stores."""

import base64
import copy
import io
import json
import sqlite3
import wave
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import av
import pytest
from docx import Document
from fastapi.testclient import TestClient
from PIL import Image
from pypdf import PdfWriter
from test_knowledge import imported
from test_knowledge import knowledge as knowledge
from test_process import server

from tianshu_memory.app import create_app
from tianshu_memory.contracts import Contracts
from tianshu_memory.domain import Fault
from tianshu_memory.knowledge_content_migration import migrate
from tianshu_memory.knowledge_migration import migrate as migrate_knowledge
from tianshu_memory.knowledge_sources import content_hash
from tianshu_memory.store import Store

PATH = "/internal/v1/knowledge/content/"
TOKEN = {"Authorization": "Bearer test-only-companion-secret"}


@pytest.fixture
def content(h):
    h.contracts = h.service.contracts = h.auth.contracts = Contracts(h.contracts.directory)
    h.contracts.load_sources()
    h.contracts.load_content()
    h.store.migrate_profiles(h.directory / "profile.sqlite")
    h.store.migrate_sources(h.directory / "sources.sqlite", h.contracts)
    migrate_knowledge(h.store, h.directory / "knowledge.sqlite")
    migrate(h.store, h.directory / "content.sqlite")
    caller = h.config["callers"]["companion"]
    caller["operations"] += [
        "content_" + n
        for n in ("acquire", "read", "original", "uploads", "upload_status", "access")
    ]
    caller.update(runtime_content=True)
    caller["allowed_actors"].append("actor:solo")
    h.save_config()
    h.client.close()
    h.client = TestClient(create_app(service=h.service, auth=h.auth))
    return h


def principal(h):
    return {"kind": "user", "query": h.query(), "scope": h.private}


def post(h, operation, body, client=None):
    response = (client or h.client).post(PATH + operation, json=body, headers=TOKEN)
    assert response.status_code == 200, response.text
    return response.json()


def image():
    output = io.BytesIO()
    Image.new("RGB", (64, 48), "blue").save(output, format="PNG")
    return output.getvalue()


def video():
    output = io.BytesIO()
    with av.open(output, "w", format="mp4") as container:
        stream = container.add_stream("mpeg4", rate=4)
        stream.width, stream.height, stream.pix_fmt = 64, 48, "yuv420p"
        for index in range(16):
            frame = av.VideoFrame.from_image(Image.new("RGB", (64, 48), (index * 12, 10, 90)))
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    return output.getvalue()


def audio():
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\x10\x01" * 32000)
    return output.getvalue()


def upload(h, raw, media, *, client=None, p=None):
    p = p or principal(h)
    descriptor = post(
        h,
        "uploads",
        {
            "principal": p,
            "filename": "synthetic",
            "media_type": media,
            "size": len(raw),
            "sha256": content_hash(raw),
        },
        client,
    )
    headers = TOKEN | {
        "Content-Type": "application/octet-stream",
        "X-Tianshu-Assertion-Ref": "origin-private",
    }
    if p["kind"] == "actor":
        headers |= {
            "X-Tianshu-Actor-Id": p["actor_id"],
            "X-Tianshu-Operation-Ref": p["operation_ref"],
        }
    response = (client or h.client).put(
        PATH + "uploads/" + descriptor["upload_id"], content=raw, headers=headers
    )
    assert response.status_code == 200, response.text
    return descriptor


def acquire_upload(h, raw, media):
    descriptor = upload(h, raw, media)
    return post(
        h,
        "acquire",
        {
            "principal": principal(h),
            "source": {"kind": "upload", "upload_id": descriptor["upload_id"]},
            "purpose": "read",
        },
    )


@contextmanager
def article_server(h):
    state = {"raw": b"<h1>Actual article</h1><p>Read the original.</p><script>hidden()</script>"}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(state["raw"])))
            self.end_headers()
            self.wfile.write(state["raw"])

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as listener:
        url = f"http://127.0.0.1:{listener.server_port}/article"
        h.config["knowledge_content"] = {"trusted_urls": [url]}
        h.save_config()
        thread = Thread(target=listener.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        try:
            yield url, state
        finally:
            listener.shutdown()
            thread.join(timeout=5)


def test_actual_http_article_version_original_permissions_and_withdrawal(content):
    h = content
    with article_server(h) as (url, state):
        request = {
            "principal": principal(h),
            "source": {"kind": "url", "url": url},
            "purpose": "read",
        }
        acquired = post(h, "acquire", request)
        ref = acquired["content_ref"]
        assert ref["sha256"] == content_hash(state["raw"]) and ref["kind"] == "article"
        read = {
            "principal": principal(h),
            "content_ref": ref,
            "range": {"unit": "characters", "start": 0, "end": ref["coverage"]["total"]},
            "budget_bytes": 4096,
        }
        body = post(h, "read", read)
        assert (
            "Read the original." in body["text"]
            and "hidden" not in body["text"]
            and body["complete"]
        )
        original = h.client.post(
            PATH + "original",
            json={"principal": principal(h), "content_ref": ref, "range": None},
            headers=TOKEN,
        )
        assert (
            original.content == state["raw"]
            and original.headers["x-source-sha256"] == ref["sha256"]
        )
        assert original.headers["cache-control"] == "no-store"
        other_audience = copy.deepcopy(read)
        other_audience["principal"] = {
            "kind": "user",
            "query": h.query("origin-group"),
            "scope": h.group,
        }
        assert h.client.post(PATH + "read", json=other_audience, headers=TOKEN).status_code == 403
        state["raw"] = b"<p>New original body.</p>"
        request["principal"] = principal(h)
        updated = post(h, "acquire", request)
        assert (
            updated["content_ref"]["object_id"] == ref["object_id"]
            and updated["content_ref"]["version"] == 2
        )
        assert h.client.post(PATH + "read", json=read, headers=TOKEN).status_code == 409
        withdrawn = post(
            h,
            "access",
            {
                "principal": principal(h),
                "content_ref": updated["content_ref"],
                "action": "withdraw",
                "reader_scope": None,
                "expected_access_version": updated["access_version"],
            },
        )
        assert withdrawn["state"] == "withdrawn"
        assert (
            h.client.post(
                PATH + "original",
                json={
                    "principal": principal(h),
                    "content_ref": updated["content_ref"],
                    "range": None,
                },
                headers=TOKEN,
            ).status_code
            == 404
        )
        with h.store.transaction() as db:
            assert (
                db.execute(
                    "SELECT COUNT(*) FROM knowledge_versions WHERE document_id=?",
                    (ref["object_id"],),
                ).fetchone()[0]
                == 2
            )
            assert (
                db.execute(
                    "SELECT project_id FROM knowledge_documents WHERE id=?", (ref["object_id"],)
                ).fetchone()[0]
                is None
            )


@pytest.mark.parametrize(
    "raw,mime,unit,end,kind,gap",
    [
        (image(), "image/png", "bytes", None, "image", None),
        (video(), "video/mp4", "seconds", 3, "video_frame", "frames_sampled"),
        (audio(), "audio/wav", "seconds", 1, "audio_clip", "audio_not_transcribed"),
    ],
    ids=["image", "video", "audio"],
)
def test_upload_decodes_real_image_video_or_audio(content, raw, mime, unit, end, kind, gap):
    h = content
    ref = acquire_upload(h, raw, mime)["content_ref"]
    result = post(
        h,
        "read",
        {
            "principal": principal(h),
            "content_ref": ref,
            "range": {"unit": unit, "start": 0, "end": end or len(raw)},
            "budget_bytes": 1048576,
        },
    )
    representations = result["representations"]
    assert representations and all(
        r["kind"] == kind and r["source_sha256"] == content_hash(raw) for r in representations
    )
    for representation in representations:
        actual = base64.b64decode(representation["data_base64"])
        assert representation["sha256"] == content_hash(actual)
        if kind != "audio_clip":
            with Image.open(io.BytesIO(actual)) as decoded:
                assert decoded.width > 0
        else:
            with wave.open(io.BytesIO(actual)) as wav:
                assert wav.getnframes() > 0 and wav.getframerate() == 16000
    if gap:
        assert gap in result["gaps"] and result["complete"] is False
    if kind == "video_frame":
        assert 1 < len(representations) <= 8 and all(
            0 <= r["at_seconds"] < 3 for r in representations
        )
        small = post(
            h,
            "read",
            {
                "principal": principal(h),
                "content_ref": ref,
                "range": {"unit": "seconds", "start": 0, "end": 3},
                "budget_bytes": 1024,
            },
        )
        assert (
            sum(len(base64.b64decode(r["data_base64"])) for r in small["representations"]) <= 1024
        )
        assert small["gaps"] and small["complete"] is False


def test_pdf_actual_page_gap_and_docx_paragraphs(content):
    writer, output = PdfWriter(), io.BytesIO()
    writer.add_blank_page(width=72, height=72)
    writer.write(output)
    ref = acquire_upload(content, output.getvalue(), "application/pdf")["content_ref"]
    result = post(
        content,
        "read",
        {
            "principal": principal(content),
            "content_ref": ref,
            "range": {"unit": "pages", "start": 0, "end": 1},
            "budget_bytes": 4096,
        },
    )
    assert result["coverage"] == {"unit": "pages", "start": 0, "end": 1}
    assert "no_text_layer" in result["gaps"] and not result["complete"]
    document, output = Document(), io.BytesIO()
    document.add_paragraph("Actual office paragraph.")
    document.save(output)
    ref = acquire_upload(
        content,
        output.getvalue(),
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )["content_ref"]
    result = post(
        content,
        "read",
        {
            "principal": principal(content),
            "content_ref": ref,
            "range": {"unit": "characters", "start": 0, "end": ref["coverage"]["total"]},
            "budget_bytes": 4096,
        },
    )
    assert result["text"] == "Actual office paragraph."


def test_actor_solo_real_url_has_no_fake_origin_and_revoked_actor_denied(content):
    h = content
    actor = {
        "kind": "actor",
        "request_id": "solo:1",
        "actor_id": "actor:solo",
        "operation_ref": "activity:actual",
    }
    with article_server(h) as (url, _):
        result = post(
            h,
            "acquire",
            {"principal": actor, "source": {"kind": "url", "url": url}, "purpose": "read"},
        )
        read = {
            "principal": dict(actor, request_id="solo:2"),
            "content_ref": result["content_ref"],
            "range": {"unit": "characters", "start": 0, "end": 5},
            "budget_bytes": 1024,
        }
        assert post(h, "read", read)["text"]
        user_read = dict(read, principal=principal(h))
        assert h.client.post(PATH + "read", json=user_read, headers=TOKEN).status_code == 403
        forged = copy.deepcopy(read)
        forged["principal"]["actor_id"] = "actor:not-managed"
        assert h.client.post(PATH + "read", json=forged, headers=TOKEN).status_code == 403
        from tianshu_memory.role_grants import RoleGrants

        h.auth.role_grants = RoleGrants(str(h.directory / "role-grants.sqlite"))
        h.auth.role_grants.apply(
            {
                "request_id": "disable:solo",
                "actor_id": "actor:solo",
                "expected_version": 0,
                "enabled": False,
                "legacy": True,
            },
            {"actor:solo"},
        )
        assert h.client.post(PATH + "read", json=read, headers=TOKEN).status_code == 403
        h.config["callers"]["companion"]["runtime_content"] = False
        h.save_config()
        assert h.client.post(PATH + "read", json=read, headers=TOKEN).status_code == 403


def test_real_restart_pending_upload_retry_same_bytes_and_read(content):
    h, raw = content, image()
    request = {
        "principal": principal(h),
        "filename": "synthetic.png",
        "media_type": "image/png",
        "size": len(raw),
        "sha256": content_hash(raw),
    }
    with server(h.config_path) as client:
        descriptor = post(h, "uploads", request, client)
        headers = TOKEN | {
            "Content-Type": "application/octet-stream",
            "X-Tianshu-Assertion-Ref": "origin-private",
        }
        broken = client.put(
            PATH + "uploads/" + descriptor["upload_id"], content=raw[:20], headers=headers
        )
        assert broken.status_code == 400
    with server(h.config_path) as client:
        status = post(
            h,
            "upload-status",
            {"principal": principal(h), "upload_id": descriptor["upload_id"]},
            client,
        )
        assert status["state"] == "pending"
        assert (
            client.put(
                PATH + "uploads/" + descriptor["upload_id"], content=raw, headers=headers
            ).json()["state"]
            == "complete"
        )
        assert (
            client.put(
                PATH + "uploads/" + descriptor["upload_id"], content=raw, headers=headers
            ).json()["state"]
            == "complete"
        )
        acquired = post(
            h,
            "acquire",
            {
                "principal": principal(h),
                "source": {"kind": "upload", "upload_id": descriptor["upload_id"]},
                "purpose": "save",
            },
            client,
        )
    with server(h.config_path) as client:
        original = client.post(
            PATH + "original",
            json={"principal": principal(h), "content_ref": acquired["content_ref"], "range": None},
        )
        assert original.content == raw
    with h.store.transaction() as db:
        assert (
            db.execute(
                "SELECT raw,state FROM knowledge_content_uploads WHERE id=?",
                (descriptor["upload_id"],),
            ).fetchone()["raw"]
            is None
        )


def test_size_bad_codec_digest_and_authority_failure_do_not_import(content):
    h = content
    too_big = {
        "principal": principal(h),
        "filename": "large.png",
        "media_type": "image/png",
        "size": 33554433,
        "sha256": "a" * 64,
    }
    assert h.client.post(PATH + "uploads", json=too_big, headers=TOKEN).status_code == 400
    assert (
        h.client.post(PATH + "uploads", json=dict(too_big, size=10), headers={}).status_code == 401
    )
    descriptor = upload(h, b"bad image bytes", "image/png")
    response = h.client.post(
        PATH + "acquire",
        json={
            "principal": principal(h),
            "source": {"kind": "upload", "upload_id": descriptor["upload_id"]},
            "purpose": "read",
        },
        headers=TOKEN,
    )
    assert response.status_code == 415
    with h.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM knowledge_content_documents").fetchone()[0] == 0


def test_migration_preserves_populated_project_original_and_guard(content, tmp_path):
    h = content
    # Existing nonempty original FK graph is also checked independently at migration time.
    with h.store.transaction() as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    old = tmp_path / "old.sqlite"
    import shutil

    shutil.copyfile(h.directory / "content.sqlite", old)
    guard = tmp_path / "guard.json"
    shutil.copyfile(h.store.recovery_path, guard)
    with pytest.raises((Fault, ValueError)):
        Store(old, recovery_path=guard)
    with sqlite3.connect(h.store.path) as db:
        triggers = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
        assert "source_revision_knowledge_content_grants_UPDATE" in triggers


def test_content_contract_positive_negative_and_pin(contracts):
    contracts.load_content()
    directory = contracts.directory.parent.parent / "knowledge-content/v1"
    for example in json.loads((directory / "examples.json").read_bytes()):
        contracts.validate("knowledge-content#" + example["definition"], example["document"])
    from jsonschema.exceptions import ValidationError

    for example in json.loads((directory / "negative-examples.json").read_bytes()):
        with pytest.raises(ValidationError):
            contracts.validate("knowledge-content#" + example["definition"], example["document"])


def test_populated_project_migration_keeps_exact_original_blocks_catalog_and_revision(knowledge):
    run, _, store, _, _ = knowledge
    receipt = imported(run)
    with store.transaction() as db:
        original = dict(
            db.execute(
                "SELECT * FROM knowledge_versions WHERE document_id=?", (receipt["document_id"],)
            ).fetchone()
        )
        blocks = [
            tuple(row)
            for row in db.execute(
                "SELECT * FROM knowledge_blocks WHERE document_id=?", (receipt["document_id"],)
            )
        ]
        revision = int(
            db.execute("SELECT value FROM metadata WHERE key='source_revision'").fetchone()[0]
        )
    result = migrate(store, str(store.path) + ".content-backup")
    # Approved restore rehearsal in a NEW isolated path: the pre-upgrade paired snapshot
    # reopens with its original nonempty Knowledge graph and original authority watermark.
    import shutil

    restored = str(store.path) + ".restore-rehearsal"
    restored_guard = restored + ".source-guard.json"
    shutil.copyfile(result["backup"], restored)
    shutil.copyfile(result["guard_backup"], restored_guard)
    snapshot = Store(restored, recovery_path=restored_guard)
    with snapshot.transaction() as db:
        assert (
            dict(
                db.execute(
                    "SELECT * FROM knowledge_versions WHERE document_id=?",
                    (receipt["document_id"],),
                ).fetchone()
            )
            == original
        )
        assert [
            tuple(row)
            for row in db.execute(
                "SELECT * FROM knowledge_blocks WHERE document_id=?", (receipt["document_id"],)
            )
        ] == blocks
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert (
            int(db.execute("SELECT value FROM metadata WHERE key='source_revision'").fetchone()[0])
            == revision
        )
        assert "knowledge_content_schema" not in dict(db.execute("SELECT key,value FROM metadata"))
    with store.transaction() as db:
        from tianshu_memory.knowledge_catalog_migration import inspect

        assert inspect(db) == []
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert original == dict(
            db.execute(
                "SELECT * FROM knowledge_versions WHERE document_id=?", (receipt["document_id"],)
            ).fetchone()
        )
        assert blocks == [
            tuple(row)
            for row in db.execute(
                "SELECT * FROM knowledge_blocks WHERE document_id=?", (receipt["document_id"],)
            )
        ]
        assert (
            int(db.execute("SELECT value FROM metadata WHERE key='source_revision'").fetchone()[0])
            > revision
        )
    assert run("query", {"text": "receipts", "budget_bytes": 8192})


def test_cross_person_requires_explicit_grant_then_revoke_blocks_same_ref(content):
    h = content
    acquired = acquire_upload(h, image(), "image/png")
    first = dict(h.private, person_id=None, conversation_id=None)
    other_account = {"namespace": "qq", "immutable_account_id": "10002"}
    h.add_origin("origin-other-register", first, other_account)
    h.save_config()
    person = h.post(
        "identity/register",
        {"command": h.command("origin-other-register"), "account": other_account},
    ).json()["person_id"]
    scope = dict(h.private, person_id=person)
    h.add_origin("origin-other", scope, other_account)
    h.save_config()
    read = {
        "principal": {"kind": "user", "query": h.query("origin-other"), "scope": scope},
        "content_ref": acquired["content_ref"],
        "range": {"unit": "bytes", "start": 0, "end": len(image())},
        "budget_bytes": 4096,
    }
    assert h.client.post(PATH + "read", json=read, headers=TOKEN).status_code == 403
    grant = post(
        h,
        "access",
        {
            "principal": principal(h),
            "content_ref": acquired["content_ref"],
            "action": "grant",
            "reader_scope": scope,
            "expected_access_version": 1,
        },
    )
    assert post(h, "read", read)["representations"]
    post(
        h,
        "access",
        {
            "principal": principal(h),
            "content_ref": acquired["content_ref"],
            "action": "revoke",
            "reader_scope": scope,
            "expected_access_version": grant["access_version"],
        },
    )
    assert h.client.post(PATH + "read", json=read, headers=TOKEN).status_code == 403


def test_read_rechecks_withdrawal_after_decoder_before_serving(content, monkeypatch):
    from tianshu_memory import knowledge_media

    h = content
    acquired = acquire_upload(h, image(), "image/png")
    original = knowledge_media.read

    def decode_then_withdraw(*args):
        decoded = original(*args)
        request = {
            "principal": principal(h),
            "content_ref": acquired["content_ref"],
            "action": "withdraw",
            "reader_scope": None,
            "expected_access_version": 1,
        }
        application = h.client.app.state.knowledge_content
        authority = application.authorize(
            request["principal"], "companion", h.config["callers"]["companion"]
        )
        application.access(request, authority)
        return decoded

    monkeypatch.setattr(knowledge_media, "read", decode_then_withdraw)
    response = h.client.post(
        PATH + "read",
        json={
            "principal": principal(h),
            "content_ref": acquired["content_ref"],
            "range": {"unit": "bytes", "start": 0, "end": len(image())},
            "budget_bytes": 4096,
        },
        headers=TOKEN,
    )
    assert response.status_code == 404 and "data_base64" not in response.text
