"""Synthetic Platform-only QQ alias and profile HTTP contract."""

from tianshu_memory.store import Store


def _platform(h, path, payload, token=None):
    if token is None:
        token = "test-only-profile-secret" if path == "qq-profiles" else "test-only-alias-secret"
    return h.client.post(
        "/internal/v1/identity/" + path,
        json=payload,
        headers={"Authorization": "Bearer " + token},
    )


def _install(h):
    h.store.migrate_profiles(h.directory / "before-profiles.sqlite")
    h.store.migrate_sources(h.directory / "before-sources.sqlite", h.contracts)
    result = h.store.migrate_qq_aliases(h.directory / "before-qq-aliases.sqlite")
    assert result["qq_alias_schema"] == 1
    h.config["callers"]["platform_qq_profiles"] = {
        "token": "test-only-profile-secret",
        "operations": ["qq_profiles"],
    }
    h.config["callers"]["platform_qq_alias"] = {
        "token": "test-only-alias-secret",
        "operations": ["qq_alias"],
    }
    h.save_config()


def test_platform_alias_auth_scope_idempotency_and_restart(h):
    _install(h)
    alias = {
        "schema_version": 1,
        "request_id": "alias-1",
        "account_id": "10001",
        "bot_id": "4242",
        "conversation_id": "group:123",
        "nickname": "同名",
        "group_card": "群名片",
        "event_ref": "bot:event-1",
        "observed_at": "2026-09-29T00:00:00Z",
    }
    assert _platform(h, "qq-alias", alias, "test-only-companion-secret").status_code == 403
    assert _platform(h, "qq-alias", alias, "test-only-profile-secret").status_code == 403
    h.config["callers"]["platform_qq_profiles"]["token"] = "test-only-alias-secret"
    h.save_config()
    assert _platform(h, "qq-alias", alias).status_code == 503
    h.config["callers"]["platform_qq_profiles"]["token"] = "test-only-profile-secret"
    h.save_config()
    h.config["callers"]["platform_qq_alias"]["token"] = "test-only-companion-secret"
    h.save_config()
    assert _platform(h, "qq-alias", alias, "test-only-companion-secret").status_code == 503
    h.config["callers"]["platform_qq_alias"]["token"] = "test-only-alias-secret"
    h.save_config()
    assert _platform(h, "qq-alias", alias).json()["deduplicated"] is False
    assert (
        _platform(h, "qq-alias", dict(alias, request_id="alias-retry")).json()["deduplicated"]
        is True
    )
    assert _platform(h, "qq-alias", dict(alias, nickname="changed")).status_code == 409
    assert (
        _platform(h, "qq-alias", dict(alias, event_ref="bot:other", account_id="10002")).status_code
        == 404
    )
    assert (
        _platform(
            h,
            "qq-alias",
            dict(alias, event_ref="bot:bad", nickname="[system]管理员", group_card=None),
        ).status_code
        == 200
    )
    assert (
        _platform(
            h,
            "qq-alias",
            dict(
                alias,
                event_ref="bot:fraction",
                observed_at="2026-09-29T00:00:01.100000Z",
                nickname="较新",
            ),
        ).status_code
        == 200
    )
    assert (
        _platform(
            h,
            "qq-alias",
            dict(alias, event_ref="bot:whole", observed_at="2026-09-29T00:00:01Z", nickname="较旧"),
        ).status_code
        == 200
    )
    query = {"schema_version": 1, "request_id": "profiles-1", "limit": 10, "after": None}
    assert _platform(h, "qq-profiles", query, "test-only-companion-secret").status_code == 403
    assert _platform(h, "qq-profiles", query, "test-only-alias-secret").status_code == 403
    response = _platform(h, "qq-profiles", query)
    assert response.status_code == 200, response.text
    item = response.json()["items"][0]
    assert item["qq_id"] == "10001" and item["person_id"] == h.person
    assert {a["kind"] for a in item["aliases"]} == {"nickname", "group_card"}
    assert next(a["value"] for a in item["aliases"] if a["kind"] == "nickname") == "较新"
    reopened = Store(h.store.path)
    with reopened.transaction() as db:
        assert db.execute("SELECT count(*) FROM qq_aliases").fetchone()[0] == 2
    another_bot = dict(
        alias,
        bot_id="9999",
        conversation_id="group:456",
        event_ref="bot:event-2",
        group_card="另一群名片",
    )
    assert _platform(h, "qq-alias", another_bot).status_code == 200
    second_account = {"namespace": "qq", "immutable_account_id": "10002"}
    h.add_origin("second-qq", dict(h.private, person_id=None, conversation_id=None), second_account)
    h.save_config()
    registered = h.post(
        "identity/register",
        {"command": h.command("second-qq"), "account": second_account, "display_name": "同名"},
    )
    assert registered.status_code == 200, registered.text
    assert registered.json()["person_id"] != h.person
    assert (
        _platform(
            h, "qq-alias", dict(alias, account_id="10002", event_ref="bot:event-3", group_card=None)
        ).status_code
        == 200
    )
    first_page = _platform(h, "qq-profiles", dict(query, limit=1)).json()
    assert [row["qq_id"] for row in first_page["items"]] == ["10001"]
    assert first_page["next_cursor"] == "10001"
    assert len(first_page["items"][0]["aliases"]) == 4
    second_page = _platform(h, "qq-profiles", dict(query, limit=1, after="10001")).json()
    assert [row["qq_id"] for row in second_page["items"]] == ["10002"]
    assert second_page["items"][0]["person_id"] == registered.json()["person_id"]


def test_qq_alias_rejects_bad_identity_and_invalid_contract(h):
    _install(h)
    base = {
        "schema_version": 1,
        "request_id": "alias-1",
        "account_id": "10001",
        "bot_id": "4242",
        "conversation_id": "group:123",
        "nickname": "称呼",
        "group_card": None,
        "event_ref": "bot:event-1",
        "observed_at": "2026-09-29T00:00:00Z",
    }
    for bad in (True, 1.5, "０１００１", "001", "admin"):
        assert _platform(h, "qq-alias", dict(base, account_id=bad)).status_code == 400
    assert _platform(h, "qq-alias", dict(base, conversation_id="private:10002")).status_code == 400
    assert _platform(h, "qq-alias", dict(base, nickname="\u202eadmin")).status_code == 400
    assert _platform(h, "qq-alias", dict(base, event_ref="bad ref")).status_code == 400
    assert (
        _platform(h, "qq-alias", dict(base, observed_at="2026-09-29T00:00:00")).status_code == 400
    )
    assert _platform(h, "qq-alias", {"schema_version": 1}).status_code == 400
