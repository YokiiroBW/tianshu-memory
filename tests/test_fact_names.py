"""Author-name interoperability over existing sourced records, without a migration."""

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from test_automatic_facts import add, initialize
from test_candidate_extraction import PATH
from test_group_context import cat, owner, query, recalled
from test_group_context import room as room
from test_memory_context import query_request
from test_source_sync import certificates as certificates
from test_source_sync import source_contracts as source_contracts
from test_source_sync import source_examples as source_examples
from test_source_sync import sync as sync
from test_storage_backends import postgres_store

from tianshu_memory import fact_names
from tianshu_memory.automatic_facts import known
from tianshu_memory.candidate_extraction import CandidateExtraction
from tianshu_memory.domain import Fault, canonical, utc
from tianshu_memory.memory_context.migration import target_key

ENGLISH_SUMMARIES = [
    ("My name is O'Neil.", "The author's name is O'Neil.", True),
    ("My name is O'Neil.", "The user's nickname is O'Neil.", True),
    ("I am called O'Neil.", "The user is called O'Neil.", True),
    ("My name is O'Neil.", "My father's name is O'Neil.", False),
    ("My name is O'Neil.", "Another user's name is O'Neil.", False),
    ("My father's name is O'Neil.", "The author's name is O'Neil.", False),
    ("If my name is O'Neil, I can enter.", "The user's name is O'Neil.", False),
    ('He said, "My name is O\'Neil."', "The author's name is O'Neil.", False),
]


def draft(text, value, key, *, quote=None, statement=None, **changes):
    fact = dict(
        key=key,
        value=value,
        state="current",
        valid_from=None,
        valid_to=None,
        keywords=["姓名", "名字"],
        entity=None,
    )
    fact.update(changes.pop("fact", {}))
    memory = dict(
        statement=statement or text,
        conditions=[],
        negations=[],
        valid_time="未说明有效时间",
        evidence=[dict(source_index=0, quote=quote or text)],
        fact=fact,
    )
    memory.update(changes)
    extraction = CandidateExtraction(
        SimpleNamespace(source_identity=lambda source, scope: "source")
    )
    return extraction._drafts(
        dict(reality="real", scope={"person_id": "author"}, sources=[{}]),
        [dict(text=text, sent_at="2026-10-11T00:00:01Z")],
        dict(memories=[memory]),
    )[0]


@pytest.mark.parametrize("key", fact_names.KEYS)
@pytest.mark.parametrize(
    "text,value",
    [
        ("我叫阿川，我家猫叫栗团，刚睡醒就蹲到窗边去了。", "阿川"),
        ("我的名字叫小禾。", "小禾"),
        ("大家叫我小宁。", "小宁"),
        ("我叫“不想上班？今天不行”。", "“不想上班？今天不行”"),
        ("我叫“不叫小禾”。", "不叫小禾"),
        ("我叫阿“川”。", "阿“川”"),
        ("My name is O'Neil.", "O'Neil"),
    ],
)
def test_preparation_uses_one_canonical_target_and_preserves_literal_name(key, text, value):
    prepared = draft(text, value, key)
    assert prepared["fact"]["key"] == fact_names.KEY
    assert prepared["fact"]["value"] == value
    assert prepared["field_key"] == "automatic.identity.name"
    assert prepared["category"] == "evidence"
    assert prepared["scope"] == {"person_id": "author"}
    assert target_key(prepared) == target_key(dict(prepared, field_key="automatic.identity.name"))


@pytest.mark.parametrize("key", fact_names.KEYS)
@pytest.mark.parametrize(
    "text,value,changes",
    [
        ("朋友叫阿川", "阿川", {}),
        ("我的猫叫阿川", "阿川", {}),
        ("这本书叫阿川", "阿川", {}),
        ("他说：“我叫阿川”。", "阿川", {"quote": "我叫阿川", "statement": "我叫阿川"}),
        ("“我叫阿川”", "阿川", {}),
        ("朋友说，我叫阿川", "阿川", {"quote": "我叫阿川", "statement": "我叫阿川"}),
        ("如果下雨，我叫阿川", "阿川", {"quote": "我叫阿川", "statement": "我叫阿川"}),
        ("假如我叫阿川", "阿川", {}),
        ("我不叫阿川", "阿川", {}),
        ("My name is not Alex.", "not Alex", {}),
        ("我是医生", "医生", {}),
        ("我以前是医生", "医生", {"fact": {"state": "historical"}}),
        ("我叫阿川吗？", "阿川", {}),
        ("我叫什么", "什么", {}),
        ("我叫你帮我拿水", "你帮我拿水", {}),
        ("我叫阿川", "小禾", {}),
        ("我叫阿川", "阿川", {"quote": "阿川", "statement": "我叫阿川"}),
        ("我叫阿川", "阿川", {"conditions": ["下雨时"]}),
        ("我叫阿川", "阿川", {"negations": ["不叫小禾"]}),
        ("我以前叫阿川", "阿川", {}),
    ],
)
def test_even_canonical_name_requires_positive_author_self_source(key, text, value, changes):
    with pytest.raises(Fault) as error:
        draft(text, value, key, **changes)
    assert error.value.code == "invalid_input"


def test_owned_objects_do_not_enter_the_author_name_family():
    entity = dict(id=None, kind="cat", mention="我的猫")
    prepared = draft("我的猫叫栗团", "栗团", "name", fact={"entity": entity})
    assert prepared["fact"]["key"] == "name" and prepared["fact"]["entity"] == entity
    with pytest.raises(Fault):
        draft("我的猫叫栗团", "栗团", "identity.name", fact={"entity": entity})
    assert draft("我喜欢阿川这个名字", "阿川", "favorite.name")["fact"]["key"] == "favorite.name"


def stored_name(h, text, key, value, *, statement=None, retain_source=True, **kwargs):
    """Synthetic stored aliases with explicit full-source proof, or genuine old NULL.

    This is not a legacy backfill: production never invents source_text for an
    existing row. All retained text here is the fixture's registered source.
    """
    if statement is not None:
        kwargs["memories"] = [
            dict(
                statement=statement,
                conditions=[],
                negations=[],
                valid_time="当前",
                evidence=[dict(source_index=0, quote=text)],
                fact=dict(
                    key="legacy.person",
                    value=value,
                    state="current",
                    valid_from=None,
                    valid_to=None,
                    keywords=["姓名", "名字"],
                    entity=None,
                ),
            )
        ]
    result, job = add(h, text, "legacy.person", value, keywords=["姓名", "名字"], **kwargs)
    identity = result["group_ids"][0]
    with h.store.transaction() as db:
        row = db.execute(
            "SELECT payload FROM automatic_facts WHERE group_id=?", (identity,)
        ).fetchone()
        payload = dict(json.loads(row[0]), key=key)
        db.execute(
            "UPDATE automatic_facts SET fact_key=?,payload=? WHERE group_id=?",
            (key, canonical(payload), identity),
        )
        db.execute("UPDATE groups SET field_key=? WHERE id=?", ("automatic." + key, identity))
        if retain_source:
            db.execute(
                "UPDATE automatic_evidence SET source_text=? WHERE group_id=?",
                (text, identity),
            )
        db.execute(
            "UPDATE context_applications SET target_key=? WHERE group_id=?",
            (
                target_key(dict(category="evidence", field_key="automatic." + key, item_key=None)),
                identity,
            ),
        )
    return result, job


def fields(h, value="automatic.identity.name"):
    result = []
    for path in ("memory/select", "memory/context/query"):
        request = (
            query_request(h, query_text="field:" + value, selection=["evidence"])
            if path.endswith("context/query")
            else dict(
                h.selection(budget=100000), query_text="field:" + value, selection=["evidence"]
            )
        )
        response = h.post(path, request)
        assert response.status_code == 200, response.text
        result.append({unit["record_id"] for unit in response.json()["selected_units"]})
    return result


def enable_context(h):
    initialize(h)
    h.service.contracts.load_context()
    h.config["callers"]["companion"]["operations"].append("context_query")
    h.save()


@pytest.mark.parametrize("key", fact_names.KEYS)
@pytest.mark.parametrize("text,statement,accepted", ENGLISH_SUMMARIES)
def test_english_summary_drafts_still_require_the_author_self_source(
    key, text, statement, accepted
):
    if accepted:
        assert draft(text, "O'Neil", key, statement=statement)["fact"]["key"] == fact_names.KEY
    else:
        with pytest.raises(Fault) as error:
            draft(text, "O'Neil", key, statement=statement)
        assert error.value.code == "invalid_input"


@pytest.mark.parametrize("text,statement,accepted", ENGLISH_SUMMARIES)
def test_english_summary_legacy_view_still_requires_the_author_self_source(
    sync, text, statement, accepted
):
    h = sync
    enable_context(h)
    original, _ = stored_name(h, text, "person.name", "O'Neil", statement=statement)
    expected = set(original["record_ids"]) if accepted else set()
    assert fields(h) == [expected, expected]
    with h.store.transaction() as db:
        names = [item for item in known(h.service, db, h.scope()) if item["key"] == fact_names.KEY]
        assert [item["value"] for item in names] == (["O'Neil"] if accepted else [])


@pytest.mark.parametrize("key", fact_names.KEYS)
@pytest.mark.parametrize("retain_source", [False, True])
def test_cropped_reported_speech_never_becomes_an_author_name(sync, key, retain_source):
    h = sync
    enable_context(h)
    original, _ = stored_name(
        h,
        "他说：“我叫阿川”。",
        key,
        "阿川",
        statement="作者名叫阿川",
        retain_source=retain_source,
    )
    with h.store.transaction() as db:
        db.execute(
            "UPDATE automatic_evidence SET quote='我叫阿川' WHERE group_id=?",
            (original["group_ids"][0],),
        )
        before = tuple(
            db.execute(
                "SELECT * FROM automatic_facts WHERE group_id=?", (original["group_ids"][0],)
            ).fetchone()
        )
    assert fields(h) == [set(), set()]
    with h.store.transaction() as db:
        assert not [
            item for item in known(h.service, db, h.scope()) if item["key"] == fact_names.KEY
        ]
        assert before == tuple(
            db.execute(
                "SELECT * FROM automatic_facts WHERE group_id=?", (original["group_ids"][0],)
            ).fetchone()
        )


@pytest.mark.parametrize("key", fact_names.KEYS)
def test_unretained_legacy_name_is_not_backfilled_or_merged_by_a_new_self_report(sync, key):
    h = sync
    enable_context(h)
    old, _ = stored_name(h, "我叫阿川", key, "阿川", retain_source=False)
    with h.store.transaction() as db:
        old_fact = tuple(
            db.execute(
                "SELECT * FROM automatic_facts WHERE group_id=?", (old["group_ids"][0],)
            ).fetchone()
        )
    assert fields(h) == [set(), set()]
    fresh, _ = add(h, "我的名字叫阿川", "author.name", "阿川", second=2)
    assert fresh["record_ids"] != old["record_ids"]
    assert fields(h) == [set(fresh["record_ids"])] * 2
    with h.store.transaction() as db:
        assert old_fact == tuple(
            db.execute(
                "SELECT * FROM automatic_facts WHERE group_id=?", (old["group_ids"][0],)
            ).fetchone()
        )
        assert (
            db.execute(
                "SELECT source_text FROM automatic_evidence WHERE group_id=?",
                (old["group_ids"][0],),
            ).fetchone()[0]
            is None
        )
        hints = [item for item in known(h.service, db, h.scope()) if item["key"] == fact_names.KEY]
        assert [item["group_id"] for item in hints] == fresh["group_ids"]


def test_original_name_text_is_server_generated_and_never_copied_for_ordinary_facts(sync):
    for changes in (
        {"source_text": "我叫阿川"},
        {"evidence": [dict(source_index=0, quote="我叫阿川", source_text="我叫阿川")]},
    ):
        with pytest.raises(Fault):
            draft("我叫阿川", "阿川", fact_names.KEY, **changes)
    h = sync
    enable_context(h)
    text = "我叫阿川，我家猫叫栗团。"
    prepared = draft(text, "阿川", "person.name", quote="我叫阿川", statement="作者名叫阿川")
    assert prepared["quotes"] == [dict(source_key="source", quote="我叫阿川", source_text=text)]
    named, _ = add(h, text, fact_names.KEY, "阿川")
    ordinary, _ = add(h, "我喜欢乌龙茶", "beverage.tea", "乌龙茶", second=2)
    with h.store.transaction() as db:
        assert (
            db.execute(
                "SELECT source_text FROM automatic_evidence WHERE group_id=?",
                (named["group_ids"][0],),
            ).fetchone()[0]
            == text
        )
        assert (
            db.execute(
                "SELECT source_text FROM automatic_evidence WHERE group_id=?",
                (ordinary["group_ids"][0],),
            ).fetchone()[0]
            is None
        )


@pytest.mark.parametrize("existing_column", [False, True])
def test_explicit_name_source_migration_keeps_old_rows_null_and_is_idempotent(
    room, existing_column
):
    h = room
    original, _ = stored_name(h, "我叫阿川", "person.name", "阿川", retain_source=False)
    with h.store.transaction() as db:
        if not existing_column:
            db.execute("ALTER TABLE automatic_evidence DROP COLUMN source_text")
        db.execute("UPDATE metadata SET value='1' WHERE key='automatic_fact_schema'")
    # No v1 read or ordinary write may access a column the old schema lacks.
    assert fields(h) == [set(), set()]
    ordinary, _ = add(h, "我喜欢乌龙茶", "beverage.tea", "乌龙茶", second=2)
    assert fields(h, "automatic.beverage.tea") == [set(ordinary["record_ids"])] * 2
    with h.store.transaction() as db:
        before = [
            tuple(row) for row in db.execute("SELECT * FROM automatic_facts ORDER BY group_id")
        ]
    backup = h.directory / "names-v1-to-v2.backup"
    result = h.store.migrate_facts(backup)
    assert result["automatic_fact_schema"] == 2
    assert backup.is_file() and backup.with_name(backup.name + ".source-guard.json").is_file()
    if h.store.backend == "sqlite":
        with sqlite3.connect(backup) as saved:
            assert (
                saved.execute(
                    "SELECT value FROM metadata WHERE key='automatic_fact_schema'"
                ).fetchone()[0]
                == "1"
            )
    else:
        assert json.loads(backup.read_bytes())["format"] == "tianshu-memory-postgresql-backup/v1"
    with h.store.transaction() as db:
        assert before == [
            tuple(row) for row in db.execute("SELECT * FROM automatic_facts ORDER BY group_id")
        ]
        assert all(
            row[0] is None for row in db.execute("SELECT source_text FROM automatic_evidence")
        )
        revision = db.execute("SELECT value FROM metadata WHERE key='source_revision'").fetchone()[
            0
        ]
    guard = h.store.recovery_path.read_bytes()
    assert h.store.migrate_facts(backup) == dict(automatic_fact_schema=2, already_installed=True)
    assert h.store.recovery_path.read_bytes() == guard
    with h.store.transaction() as db:
        assert (
            db.execute("SELECT value FROM metadata WHERE key='source_revision'").fetchone()[0]
            == revision
        )
    assert fields(h) == [set(), set()]
    fresh, _ = add(h, "我还是叫阿川", "identity.name", "阿川", second=3)
    assert fresh["record_ids"] != original["record_ids"]
    assert fields(h) == [set(fresh["record_ids"])] * 2


def test_failed_name_source_migration_rolls_back_ddl_version_and_guard(sync, monkeypatch):
    h = sync
    enable_context(h)
    stored_name(h, "我叫阿川", "person.name", "阿川", retain_source=False)
    with h.store.transaction() as db:
        db.execute("ALTER TABLE automatic_evidence DROP COLUMN source_text")
        db.execute("UPDATE metadata SET value='1' WHERE key='automatic_fact_schema'")
    guard = h.store.recovery_path.read_bytes()
    transaction = h.store.transaction

    class FailedVersion:
        def __init__(self, db):
            self.db = db

        def execute(self, sql, parameters=()):
            if sql == "UPDATE metadata SET value='2' WHERE key='automatic_fact_schema'":
                raise RuntimeError("injected version write failure after ALTER")
            return self.db.execute(sql, parameters)

    @contextmanager
    def failed_transaction():
        with transaction() as db:
            yield FailedVersion(db)

    with monkeypatch.context() as patch:
        patch.setattr(h.store, "transaction", failed_transaction)
        with pytest.raises(RuntimeError, match="after ALTER"):
            h.store.migrate_facts(h.directory / "failed-name-upgrade.backup")
    assert h.store.recovery_path.read_bytes() == guard
    with h.store.transaction() as db:
        assert (
            db.execute("SELECT value FROM metadata WHERE key='automatic_fact_schema'").fetchone()[0]
            == "1"
        )
        assert "source_text" not in {
            row[1] for row in db.execute("PRAGMA table_info(automatic_evidence)")
        }


@pytest.mark.parametrize("version", [1, 2])
def test_postgresql_name_source_import_retains_v2_proof_and_leaves_v1_unknown(sync, version):
    from test_storage_backends import pg_options

    options = pg_options()  # Explicit isolated URL is required before touching a PG store.
    h = sync
    enable_context(h)
    text = "我叫阿川"
    named, _ = add(h, text, fact_names.KEY, "阿川")
    if version == 1:
        with h.store.transaction() as db:
            db.execute("ALTER TABLE automatic_evidence DROP COLUMN source_text")
            db.execute("UPDATE metadata SET value='1' WHERE key='automatic_fact_schema'")
    h.store.close()
    with postgres_store(h.store.path, settings=dict(h.config, storage_backends=options)) as (
        store,
        _,
    ):
        h.store = h.service.store = store
        with store.transaction() as db:
            assert db.execute(
                "SELECT value FROM metadata WHERE key='automatic_fact_schema'"
            ).fetchone()[0] == str(version)
            assert db.execute(
                "SELECT source_text FROM automatic_evidence WHERE group_id=?",
                (named["group_ids"][0],),
            ).fetchone()[0] == (text if version == 2 else None)
        assert fields(h) == ([set(named["record_ids"])] * 2 if version == 2 else [set(), set()])
        if version == 1:
            store.migrate_facts(h.directory / "imported-v1-name-proof.backup")
            assert fields(h) == [set(), set()]
        assert store.migrate_facts(h.directory / "unused-already-v2.backup") == dict(
            automatic_fact_schema=2, already_installed=True
        )


def test_postgresql_fresh_name_source_schema_starts_at_v2(tmp_path):
    with postgres_store(tmp_path / "fresh-names.sqlite") as (store, _):
        with store.transaction() as db:
            assert (
                db.execute(
                    "SELECT value FROM metadata WHERE key='automatic_fact_schema'"
                ).fetchone()[0]
                == "2"
            )
            assert list(db.execute("SELECT source_text FROM automatic_evidence")) == []


def test_unproved_group_name_never_falls_back_to_cats_and_fresh_same_owner_can_connect(room):
    h = room
    a, b = owner(h), owner(h)
    stored_name(h, "我叫阿川", "person.name", "阿川", actor=a, retain_source=False)
    first = cat(h, a, second=2)[0]
    cat(h, b, "栗糖", second=3)
    assert not recalled(h, query(h, "阿川那只猫叫什么"))["selected_units"]
    assert len(recalled(h)["selected_units"]) == 2  # The ordinary room question still works.
    fresh, _ = add(h, "我还是叫阿川", fact_names.KEY, "阿川", actor=a, second=4)
    selected = recalled(h, query(h, "阿川那只猫叫什么"))
    assert {unit["record_id"] for unit in selected["selected_units"]} == set(
        fresh["record_ids"] + first["record_ids"]
    )
    stored_name(h, "我叫阿川", "author.name", "阿川", actor=b, second=5, retain_source=False)
    assert not recalled(h, query(h, "阿川那只猫叫什么"))["selected_units"]


def test_legacy_reaffirmation_keeps_storage_key_but_unifies_known_and_target(sync):
    h = sync
    enable_context(h)
    original, _ = stored_name(h, "我叫阿川", "person.name", "阿川")
    repeated, _ = add(h, "我的名字叫阿川", "author.name", "阿川", second=2)
    assert repeated["record_ids"] == original["record_ids"]
    identity = original["group_ids"][0]
    with h.store.transaction() as db:
        before = dict(
            db.execute("SELECT * FROM automatic_facts WHERE group_id=?", (identity,)).fetchone()
        )
        hints = known(h.service, db, h.scope())
        after = dict(
            db.execute("SELECT * FROM automatic_facts WHERE group_id=?", (identity,)).fetchone()
        )
        assert before == after
        assert (
            before["fact_key"] == "person.name"
            and json.loads(before["payload"])["key"] == "person.name"
        )
        assert (
            db.execute("SELECT field_key FROM groups WHERE id=?", (identity,)).fetchone()[0]
            == "automatic.person.name"
        )
        latest = db.execute(
            "SELECT target_key FROM context_applications WHERE source_key IN "
            "(SELECT source_key FROM automatic_evidence WHERE group_id=?)",
            (identity,),
        ).fetchall()
        assert target_key(
            dict(category="evidence", field_key="automatic.identity.name", item_key=None)
        ) in {row[0] for row in latest}
    assert [(item["key"], item["value"], item["superseded"]) for item in hints] == [
        ("identity.name", "阿川", False)
    ]
    for key in fact_names.FIELDS:
        assert fields(h, key) == [set(original["record_ids"])] * 2


def test_parallel_legacy_keys_have_one_current_family_and_do_not_rewrite_on_read(sync):
    h = sync
    enable_context(h)
    old, _ = stored_name(h, "我叫阿川", "person.name", "阿川")
    current, _ = stored_name(h, "我叫小禾", "identity.name", "小禾", second=2)
    with h.store.transaction() as db:
        before = [
            tuple(row) for row in db.execute("SELECT * FROM automatic_facts ORDER BY group_id")
        ]
    for key in fact_names.FIELDS:
        assert fields(h, key) == [set(current["record_ids"])] * 2
    with h.store.transaction() as db:
        hints = known(h.service, db, h.scope())
        assert before == [
            tuple(row) for row in db.execute("SELECT * FROM automatic_facts ORDER BY group_id")
        ]
    assert {item["key"] for item in hints} == {"identity.name"}
    assert {item["value"] for item in hints if not item["superseded"]} == {"小禾"}
    conflict, _ = stored_name(
        h,
        "我叫小宁",
        "author.name",
        "小宁",
        second=3,
        sent_at="2026-10-11T00:00:02Z",
    )
    assert old["record_ids"] != conflict["record_ids"]
    assert fields(h) == [set(), set()]  # same effective time, different literal values


@pytest.mark.parametrize("newer_first", [False, True])
def test_name_correction_and_reaffirmation_follow_source_order_across_old_keys(
    sync, monkeypatch, newer_first
):
    h = sync
    enable_context(h)
    original, _ = stored_name(h, "我叫阿川", "person.name", "阿川")
    post = h.post
    latest = None

    def reaffirm():
        return add(h, "我还是叫阿川", "author.name", "阿川", second=4)[0]

    def newer(path, body):
        nonlocal latest
        if path == PATH and body.get("output") is not None and latest is None:
            latest = {}
            latest = reaffirm()
        return post(path, body)

    if newer_first:
        monkeypatch.setattr(h, "post", newer)
    changed, _ = add(h, "我现在叫小禾", "name", "小禾", second=3)
    if not newer_first:
        latest = reaffirm()
    assert fields(h) == [set(latest["record_ids"])] * 2
    with h.store.transaction() as db:
        assert (
            db.execute(
                "SELECT superseded_by FROM automatic_facts WHERE group_id=?",
                (changed["group_ids"][0],),
            ).fetchone()[0]
            == latest["group_ids"][0]
        )
        assert (
            db.execute(
                "SELECT fact_key FROM automatic_facts WHERE group_id=?",
                (original["group_ids"][0],),
            ).fetchone()[0]
            == "person.name"
        )
        hints = known(h.service, db, h.scope())
        assert {item["value"] for item in hints if not item["superseded"]} == {"阿川"}


def test_unproven_old_names_do_not_become_identity_and_ordinary_object_field_is_kept(sync):
    h = sync
    enable_context(h)
    stored_name(h, "他说：“我叫阿川”。", "identity.name", "阿川")
    pet, _ = stored_name(h, "我的猫叫栗团", "name", "栗团", second=2)
    assert fields(h) == [set(), set()]
    assert fields(h, "automatic.name") == [set(pet["record_ids"])] * 2
    with h.store.transaction() as db:
        assert not [
            item for item in known(h.service, db, h.scope()) if item["key"] == "identity.name"
        ]


def test_finite_name_family_overflow_is_not_a_prefix_winner(sync, monkeypatch):
    h = sync
    enable_context(h)
    stored_name(h, "我叫阿川", "person.name", "阿川")
    stored_name(h, "我叫小禾", "author.name", "小禾", second=2)
    monkeypatch.setattr(fact_names, "MAX_FAMILY", 1)
    assert fields(h) == [set(), set()]
    response = h.post(
        "memory/context/query",
        query_request(h, query_text="field:automatic.identity.name", selection=["evidence"]),
    )
    assert response.status_code == 200, response.text
    assert response.json()["coverage"]["complete"] is False


def test_old_key_correction_and_known_use_both_exact_paths_on_each_backend(room):
    h = room
    old, _ = stored_name(h, "我叫阿川", "person.name", "阿川")
    current, _ = add(h, "我现在叫小禾", "author.name", "小禾", second=2)
    repeated, _ = add(h, "大家叫我小禾", "name", "小禾", second=3)
    assert repeated["record_ids"] == current["record_ids"]
    for key in fact_names.FIELDS:
        assert fields(h, key) == [set(current["record_ids"])] * 2
    with h.store.transaction() as db:
        hints = known(h.service, db, h.scope())
        assert {item["value"] for item in hints if not item["superseded"]} == {"小禾"}
        assert {item["key"] for item in hints} == {"identity.name"}
        row = db.execute(
            "SELECT fact_key,superseded_by FROM automatic_facts WHERE group_id=?",
            (old["group_ids"][0],),
        ).fetchone()
        assert row["fact_key"] == "person.name"
        assert row["superseded_by"] == current["group_ids"][0]


def test_legacy_rank_rechecks_alias_name_expiry_on_each_backend(room):
    h = room
    moment = [datetime(2026, 10, 21, tzinfo=UTC)]
    h.service.clock = lambda: moment[0]
    stored_name(
        h,
        "我叫阿川，有效期截止2026-10-21T00:00:01Z",
        "person.name",
        "阿川",
        valid_to=utc(moment[0] + timedelta(seconds=1)),
    )

    class Ranker:
        def settings_for(self, request):
            return dict(mode="hybrid", candidates=100)

        def invalidate(self):
            pass

        def rank(self, query_text, candidates, lexical, settings):
            assert lexical
            moment[0] += timedelta(seconds=2)
            return lexical

    h.service.retrieval = Ranker()
    request = dict(h.selection(), query_text="名字", selection=["evidence"])
    response = h.post("memory/select", request)
    assert response.status_code == 409, response.text


def test_cold_group_name_interop_retains_real_owner_and_complete_witness(room):
    h = room
    a, b = owner(h), owner(h)
    name, _ = stored_name(h, "我叫阿川", "person.name", "阿川", actor=a)
    first = cat(h, a, second=2)[0]
    stored_name(h, "我叫小禾", "author.name", "小禾", actor=b, second=3)
    cat(h, b, "栗糖", second=4)
    selected = recalled(h, query(h, "阿川那只猫叫什么"))
    assert {unit["record_id"] for unit in selected["selected_units"]} == set(
        name["record_ids"] + first["record_ids"]
    )
    assert {unit["subject_person_id"] for unit in selected["selected_units"]} == {
        h.scope(a)["person_id"]
    }
    assert [check["scope"] for check in selected["scope_checks"]] == [h.scope(), h.scope(a)]
    assert not recalled(h, query(h, "我的猫叫什么"))["selected_units"]
    stored_name(h, "我叫阿川", "name", "阿川", actor=b, second=5)
    assert not recalled(h, query(h, "阿川那只猫叫什么"))["selected_units"]


def test_missing_entire_name_does_not_create_one_or_block_an_ordinary_group_fact(room):
    h = room
    a = owner(h)
    result = cat(h, a)[0]
    assert {unit["record_id"] for unit in recalled(h)["selected_units"]} == set(
        result["record_ids"]
    )
    with h.store.transaction() as db:
        assert db.execute("SELECT count(*) FROM automatic_facts").fetchone()[0] == 1
        assert (
            db.execute("SELECT fact_key FROM automatic_facts")
            .fetchone()[0]
            .startswith("pet.cat.name")
        )


@pytest.mark.parametrize("change", ["expiry", "same-name-start"])
def test_alias_name_currentness_is_reproved_after_external_rank(room, change):
    h = room
    moment = [datetime(2026, 10, 21, tzinfo=UTC)]
    h.service.clock = lambda: moment[0]
    a, b = owner(h), owner(h)
    stored_name(
        h,
        "我叫阿川，有效期截止2026-10-21T00:00:01Z" if change == "expiry" else "我叫阿川",
        "person.name",
        "阿川",
        actor=a,
        valid_to=utc(moment[0] + timedelta(seconds=1)) if change == "expiry" else None,
    )
    cat(h, a, second=2)
    if change == "same-name-start":
        stored_name(
            h,
            "我从2026-10-21T00:00:01Z起叫阿川",
            "author.name",
            "阿川",
            actor=b,
            second=3,
            valid_from=utc(moment[0] + timedelta(seconds=1)),
        )
    else:
        stored_name(h, "我叫小禾", "author.name", "小禾", actor=b, second=3)
    cat(h, b, "栗糖", second=4)
    assert len(recalled(h, query(h, "阿川那只猫叫什么"))["selected_units"]) == 2

    class Ranker:
        def settings_for(self, request):
            return dict(mode="hybrid", candidates=100)

        def invalidate(self):
            pass

        def rank(self, query_text, candidates, lexical, settings):
            moment[0] += timedelta(seconds=2)
            return lexical

    h.service.retrieval = Ranker()
    response = h.post("memory/context/query", query(h, "阿川那只猫叫什么"))
    assert response.status_code in {409, 503}, response.text


def test_name_evidence_and_current_family_are_batched_per_query(room, monkeypatch):
    h = room
    for index, key in enumerate(fact_names.KEYS):
        actor = owner(h)
        stored_name(h, f"我叫姓名{index}", key, f"姓名{index}", actor=actor, second=index * 2 + 1)
        cat(h, actor, f"猫名{index}", second=index * 2 + 2)
    statements = []
    if h.store.backend == "postgresql":
        from tianshu_storage.postgres import PostgresConnection

        native = PostgresConnection.native

        def observed(connection, sql, parameters=(), **kwargs):
            statements.append(sql)
            return native(connection, sql, parameters, **kwargs)

        monkeypatch.setattr(PostgresConnection, "native", observed)
    else:
        connect = h.store._connect

        def observed():
            db = connect()
            db.set_trace_callback(statements.append)
            return db

        monkeypatch.setattr(h.store, "_connect", observed)
    selected = recalled(h)
    assert len(selected["selected_units"]) == 4
    assert (
        sum(
            sql.startswith(
                "SELECT group_id,source_key,quote,source_text FROM automatic_evidence WHERE group_id IN"
            )
            for sql in statements
        )
        == 1
    )
    assert (
        sum(
            sql.startswith("SELECT l.group_id,l.source_key,s.scope FROM lineage")
            for sql in statements
        )
        == 1
    )
