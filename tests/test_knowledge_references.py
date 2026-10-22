"""Ordered reference collections, with SQLite scalar and local PG-branch equivalence.

The SQL observer delegates to the real connection. On SQLite it exercises only
the relational collection reads, never PostgreSQL tokenization or its driver.
The same cases have native PostgreSQL entry points in test_storage_backends.
"""

import copy
import hashlib

import pytest
from test_knowledge import SECRET, imported, query, state
from test_knowledge import knowledge as knowledge

from tianshu_memory import knowledge as module
from tianshu_memory.domain import Fault, canonical
from tianshu_memory.knowledge import KnowledgeApplication
from tianshu_memory.knowledge_sources import FileReader


class ReferenceReads:
    def __init__(self, db):
        self.db = db
        self.knowledge_files = db.knowledge_files
        self.statements = []

    def execute(self, sql, parameters=()):
        self.statements.append((sql, parameters))
        return self.db.execute(sql, parameters)

    def native(self, sql, parameters=()):
        self.statements.append((sql, parameters))
        return getattr(self.db, "native", self.db.execute)(sql, parameters)

    def read_batch(self, requests):
        self.statements.extend(requests)
        if hasattr(self.db, "read_batch"):
            return self.db.read_batch(requests)
        return [self.db.execute(sql, parameters) for sql, parameters in requests]

    def bodies(self):
        return [
            parameters
            for sql, parameters in self.statements
            if "b.payload" in sql and "knowledge_blocks b" in sql
        ]


def collection(knowledge, *, second=False):
    run, root, _, _, _ = knowledge
    (root / "design.md").write_text(
        "Receipt alpha.\nReceipt beta.\nReceipt gamma.\n", encoding="utf-8"
    )
    first = imported(
        run,
        groups=[dict(start=i, end=i, depends_on=[]) for i in range(1, 4)],
    )
    if second:
        (root / "second.md").write_text("Receipt delta.\nReceipt epsilon.\n", encoding="utf-8")
        imported(
            run,
            key="second",
            locator="second.md",
            groups=[dict(start=i, end=i, depends_on=[]) for i in range(1, 3)],
        )
    found = query(run, "receipt", 32768)["blocks"]
    references = [block["reference"] for block in found]
    references.sort(
        key=lambda reference: (
            reference["document_id"] != first["document_id"],
            reference["block_id"],
        )
    )
    assert len(references) == (5 if second else 3)
    return references


def outcome(application, db, project, references, *, scalar=False, current=True):
    reader = FileReader(project)
    calls = []

    def read(document, expected):
        calls.append((document["locator"], expected))
        return reader.capture(document, expected) and current

    values = []
    try:
        if scalar:
            for reference in references:
                values.append(application._reference(db, "demo", project, reference, read))
        else:
            values.extend(application._references(db, "demo", project, references, read))
        error = None
    except (Fault, ValueError, TypeError) as exception:
        error = type(exception), str(exception), getattr(exception, "status", None)
    return values, error, calls, reader.expected


@pytest.mark.parametrize(
    "case",
    ["valid", "missing_document", "version", "hash", "missing_block", "withdrawn", "file"],
)
def test_collection_matches_scalar_order_and_body_admission(knowledge, case):
    references = collection(knowledge, second=True)
    _, _, store, path, config = knowledge
    application = KnowledgeApplication(path)
    project = config["knowledge"]["projects"]["demo"]
    references = [references[1], references[0], references[1], *references[2:]]
    references = copy.deepcopy(references)
    if case == "missing_document":
        references[0]["document_id"] = "absent"
    elif case == "version":
        references[0]["version"] += 1
    elif case == "hash":
        references[0]["hash"] = "forged"
    elif case == "missing_block":
        references[0]["block_id"] = "absent"
    with store.transaction() as db:
        if case == "withdrawn":
            db.execute(
                "UPDATE knowledge_documents SET state='deleted' WHERE id=?",
                (references[0]["document_id"],),
            )
        scalar, grouped = ReferenceReads(db), ReferenceReads(db)
        expected = outcome(
            application, scalar, project, references, scalar=True, current=case != "file"
        )
        actual = outcome(application, grouped, project, references, current=case != "file")
        assert actual == expected
        if case == "valid":
            assert len(actual[0]) == len(references)
            assert all(
                value["reference"] is reference for value, reference in zip(actual[0], references)
            )
            assert len(actual[2]) == len(references)
            assert len(grouped.bodies()) == 2
            assert len(scalar.bodies()) == len(references)
            assert len(grouped.statements) == 3
        else:
            assert actual[1] is not None and not grouped.bodies()
            assert "second.md" not in actual[3]


@pytest.mark.parametrize("corruption", ["body", "provenance", "file_ref"])
@pytest.mark.parametrize("later", ["valid", "malformed", "nul", "surrogate"])
def test_corrupt_early_evidence_keeps_original_failure_before_later_capture(
    knowledge, corruption, later
):
    references = collection(knowledge, second=True)
    _, _, store, path, config = knowledge
    application = KnowledgeApplication(path)
    project = config["knowledge"]["projects"]["demo"]
    first = references[0]
    tail = references[-1]
    if later == "malformed":
        tail = {"document_id": tail["document_id"]}
    elif later in {"nul", "surrogate"}:
        tail = dict(tail, document_id="\x00" if later == "nul" else "\ud800")
    with store.transaction() as db:
        if corruption == "provenance":
            db.execute(
                "UPDATE knowledge_versions SET provenance='{' WHERE document_id=? AND version=?",
                (first["document_id"], first["version"]),
            )
        else:
            payload = (
                "{"
                if corruption == "body"
                else canonical(
                    {
                        "text_file": canonical(
                            {
                                "format": "tianshu-knowledge-file/v1",
                                "file": {"not": "a file reference"},
                            }
                        )
                    }
                )
            )
            db.execute(
                "UPDATE knowledge_blocks SET payload=? WHERE id=?", (payload, first["block_id"])
            )
        scalar, grouped = ReferenceReads(db), ReferenceReads(db)
        expected = outcome(application, scalar, project, [first, tail], scalar=True)
        actual = outcome(application, grouped, project, [first, tail])
        assert actual == expected
        assert actual[1] is not None and actual[2] == [("design.md", first["hash"])]
        assert "second.md" not in actual[3]
        assert len(grouped.bodies()) == 1


def test_collection_repeats_source_checks_and_reloads_rows_in_each_phase(knowledge):
    references = collection(knowledge)
    _, _, store, path, config = knowledge
    application = KnowledgeApplication(path)
    project = config["knowledge"]["projects"]["demo"]
    with store.transaction() as db:
        reads = ReferenceReads(db)
        calls = 0

        def second_fails(document, expected):
            nonlocal calls
            calls += 1
            return calls == 1

        with pytest.raises(Fault, match="stale_evidence"):
            list(application._references(reads, "demo", project, references, second_fails))
        assert calls == 2 and len(reads.bodies()) == 1
        first = outcome(application, reads, project, references)
        assert first[1] is None
        db.execute(
            "UPDATE knowledge_documents SET version=version+1 WHERE id=?",
            (references[0]["document_id"],),
        )
        latest = outcome(application, reads, project, references)
        assert latest[1][0] is Fault and latest[2] == []
        assert len(reads.bodies()) == 2


def test_collection_state_recovery_check_budget_and_project_isolation(knowledge):
    references = collection(knowledge)
    run, root, _, path, config = knowledge
    note = state(run)
    note["evidence"] = [references[1], references[0], references[1], references[2]]
    note["pitfalls"] = []
    run("write_state", dict(key="state", expected_version=0, state=note))
    pack = run("recover", dict(text="receipt", budget_bytes=16384))
    assert [block["reference"] for block in pack["blocks"]] == note["evidence"]
    assert run("check", {"package": pack})["valid"]
    tampered = copy.deepcopy(pack)
    tampered["blocks"][0]["reference"]["hash"] = "forged"
    assert not run("check", {"package": tampered})["valid"]
    small = run("recover", dict(text="receipt", budget_bytes=1024))
    assert small["state"] is None and "state_budget" in small["omissions"]
    assert len(canonical(small).encode()) <= 1024
    forged = copy.deepcopy(note)
    forged["evidence"][1]["hash"] = "forged"
    with pytest.raises(Fault, match="stale_evidence"):
        run("write_state", dict(key="forged", expected_version=1, state=forged))
    other = copy.deepcopy(config["knowledge"]["projects"]["demo"])
    other["root"] = str(root.parent / "other")
    (root.parent / "other").mkdir()
    config["knowledge"]["projects"]["other"] = other
    config["knowledge"]["clients"]["writer"]["projects"].append("other")
    path.write_text(canonical(config), encoding="utf-8")
    with pytest.raises(Fault, match="not_found"):
        run("write_state", dict(key="other", expected_version=0, state=note), project_id="other")
    (root / "design.md").write_text("Receipt replaced.\n", encoding="utf-8")
    assert not run("check", {"package": pack})["valid"]
    replaced = imported(run, key="replacement", expected_version=1, groups=None)
    assert replaced["version"] == 2 and not run("check", {"package": pack})["valid"]
    assert "stale_state" in run("recover", dict(text="receipt", budget_bytes=16384))["omissions"]


@pytest.mark.parametrize("operation", ["write_state", "recover", "check"])
def test_collection_file_change_between_hash_reads_is_still_rejected(
    knowledge, monkeypatch, operation
):
    references = collection(knowledge)
    run, root, _, _, _ = knowledge
    note = state(run)
    note["evidence"] = references
    note["pitfalls"] = []
    run("write_state", dict(key="state", expected_version=0, state=note))
    pack = run("recover", dict(text="receipt", budget_bytes=16384))
    read_file = module.read_file
    calls = 0

    def changed(*args):
        nonlocal calls
        raw = read_file(*args)
        calls += 1
        if calls == 1:
            (root / "design.md").write_text("Receipt changed during read.\n", encoding="utf-8")
        return raw

    monkeypatch.setattr(module, "read_file", changed)
    monkeypatch.setattr("tianshu_memory.knowledge_sources.read_file", changed)
    if operation == "write_state":
        with pytest.raises(Fault, match="stale_evidence"):
            run(operation, dict(key="state2", expected_version=1, state=note))
    elif operation == "recover":
        result = run(operation, dict(text="receipt", budget_bytes=16384))
        assert result["state"] is None and not result["blocks"]
    else:
        assert not run(operation, {"package": pack})["valid"]
    assert calls == 2


def test_collection_url_allowlist_remains_a_body_gate(knowledge, monkeypatch):
    run, _, store, path, config = knowledge
    url = "https://example.com/design"
    monkeypatch.setattr(
        module, "fetch_url", lambda *args: (b"Receipt alpha.\nReceipt beta.\n", "text/plain", url)
    )
    imported(
        run,
        kind="url",
        locator=url,
        groups=[dict(start=1, end=1, depends_on=[]), dict(start=2, end=2, depends_on=[])],
    )
    references = [block["reference"] for block in query(run, "receipt")["blocks"]]
    assert len(references) == 2
    project = copy.deepcopy(config["knowledge"]["projects"]["demo"])
    application = KnowledgeApplication(path)
    with store.transaction() as db:
        reads = ReferenceReads(db)
        assert outcome(application, reads, project, references)[1] is None
        assert len(reads.bodies()) == 1
        project["urls"] = []
        rejected = ReferenceReads(db)
        expected = outcome(application, ReferenceReads(db), project, references, scalar=True)
        assert outcome(application, rejected, project, references) == expected
        assert expected[1][0] is Fault and expected[2] == [] and not rejected.bodies()


@pytest.mark.parametrize("first", ["stale", "corrupt"])
def test_collection_check_keeps_early_failure_before_later_malformed_block(knowledge, first):
    references = collection(knowledge)
    run, _, store, path, config = knowledge
    application = KnowledgeApplication(path)
    project = config["knowledge"]["projects"]["demo"]
    secret = hashlib.sha256(SECRET.encode()).hexdigest()
    pack = run("recover", dict(text="receipt", budget_bytes=16384))
    pack["blocks"] = [{"reference": references[0]}, {}]
    if first == "stale":
        pack["blocks"][0]["reference"] = dict(references[0], hash="forged")
    pack["seal"] = application._seal(
        {key: value for key, value in pack.items() if key != "seal"}, secret
    )
    with store.transaction() as db:
        if first == "corrupt":
            db.execute(
                "UPDATE knowledge_blocks SET payload='{' WHERE id=?", (references[0]["block_id"],)
            )
            with pytest.raises(ValueError):
                application._check(
                    ReferenceReads(db), "demo", project, pack, secret, lambda *args: True
                )
        else:
            assert not application._check(
                ReferenceReads(db), "demo", project, pack, secret, lambda *args: True
            )["valid"]
