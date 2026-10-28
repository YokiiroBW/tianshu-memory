"""Owner disclosure is based on the actual fetch chain, never URL kind alone."""

import pytest
from test_auth_https import certificates as certificates
from test_knowledge_content import (
    PATH,
    TOKEN,
    acquire_upload,
    article_server,
    post,
    principal,
)
from test_knowledge_content import (
    content as content,
)
from test_knowledge_transport import source_server as source_server

from tianshu_memory.domain import Fault
from tianshu_memory.knowledge_sources import fetch_url


def disclosure(h, acquired, audience=None):
    return dict(
        schema_version=1,
        principal=principal(h),
        content_ref=acquired["content_ref"],
        audience=audience or {"kind": "public"},
    )


@pytest.mark.parametrize(
    "path,trusted,public",
    [
        ("/redirect", [], True),
        ("/ok?token=secret", [], False),
        ("/token/secret-value", [], False),
        ("/redirect", ["https://127.0.0.1/ok"], False),
    ],
)
def test_actual_chain_captures_anonymous_global_evidence(source_server, path, trusted, public):
    evidence = {}
    fetch_url("https://127.0.0.1" + path, trusted, submitted=True, disclosure_evidence=evidence)
    assert evidence["public_anonymous"] is public
    assert len(evidence["chain"]) == len(source_server)
    assert all(hop["method"] == "GET" and hop["anonymous"] for hop in evidence["chain"])


def test_upload_and_trusted_internal_url_remain_private(content):
    h = content
    uploaded = acquire_upload(h, b"Private actual bytes", "text/plain")
    result = post(h, "disclosure", disclosure(h, uploaded))
    assert result["read_allowed"] and not result["disclosure_allowed"]
    assert result["classification"] == "private"
    with article_server(h) as (url, _):
        acquired = post(
            h,
            "acquire",
            dict(principal=principal(h), source={"kind": "url", "url": url}, purpose="read"),
        )
        result = post(h, "disclosure", disclosure(h, acquired))
        assert result["classification"] == "private" and not result["disclosure_allowed"]


def test_public_acquisition_and_same_bytes_classification_keep_read_version(content, source_server):
    h = content
    url = "https://127.0.0.1/redirect"
    acquired = post(
        h,
        "acquire",
        dict(principal=principal(h), source={"kind": "url", "url": url}, purpose="read"),
    )
    first = post(h, "disclosure", disclosure(h, acquired))
    assert first["disclosure_allowed"] and first["classification"] == "public_anonymous"
    h.config["knowledge_content"] = {"trusted_urls": ["https://127.0.0.1/ok"]}
    h.save_config()
    again = post(
        h,
        "acquire",
        dict(principal=principal(h), source={"kind": "url", "url": url}, purpose="read"),
    )
    assert again["access_version"] == acquired["access_version"]
    second = post(h, "disclosure", disclosure(h, again))
    assert not second["disclosure_allowed"]
    assert second["disclosure_version"] != first["disclosure_version"]


def test_disclosure_rechecks_withdrawal_and_old_exact_scope(content):
    h = content
    acquired = acquire_upload(h, b"Private original", "text/plain")
    own = post(h, "disclosure", disclosure(h, acquired, {"kind": "user_scope", "scope": h.private}))
    assert own["disclosure_allowed"] and own["classification"] == "private"
    app = h.client.app.state.knowledge_content
    payload = disclosure(h, acquired)
    authority = app.authorize(payload["principal"], "companion", h.config["callers"]["companion"])

    def refresh():
        app.access(
            dict(
                content_ref=acquired["content_ref"],
                action="withdraw",
                reader_scope=None,
                expected_access_version=1,
            ),
            authority | {"request_id": "withdraw:race"},
        )
        return authority

    with pytest.raises(Fault, match="not_found"):
        app.disclosure(payload, authority, refresh)
    response = h.client.post(PATH + "disclosure", json=payload, headers=TOKEN)
    assert response.status_code == 404


def test_exact_scope_grant_and_revoke_change_disclosure_separately(content):
    h = content
    acquired = acquire_upload(h, b"Only this exact reader scope", "text/plain")
    payload = disclosure(h, acquired, {"kind": "user_scope", "scope": h.group})
    denied = post(h, "disclosure", payload)
    assert denied["read_allowed"] and not denied["disclosure_allowed"]
    grant = post(
        h,
        "access",
        dict(
            principal=principal(h),
            content_ref=acquired["content_ref"],
            action="grant",
            reader_scope=h.group,
            expected_access_version=1,
        ),
    )
    allowed = post(h, "disclosure", payload)
    assert allowed["disclosure_allowed"] and allowed["classification"] == "private"
    assert allowed["disclosure_version"] != denied["disclosure_version"]
    assert not post(h, "disclosure", disclosure(h, acquired))["disclosure_allowed"]
    post(
        h,
        "access",
        dict(
            principal=principal(h),
            content_ref=acquired["content_ref"],
            action="revoke",
            reader_scope=h.group,
            expected_access_version=grant["access_version"],
        ),
    )
    revoked = post(h, "disclosure", payload)
    assert not revoked["disclosure_allowed"]
    assert revoked["disclosure_version"] != allowed["disclosure_version"]
