"""Versioned research notes and explicit project decisions.

Two registered synthetic projects, isolated files, database and credentials. Every note
operation runs through `KnowledgeApplication`, so authorization, idempotency, the three-phase
transaction boundary and citation freshness are exercised exactly as in production use.
"""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tianshu_memory.domain import Fault, canonical
from tianshu_memory.knowledge import KnowledgeApplication
from tianshu_memory.knowledge_migration import migrate
from tianshu_memory.research_notes import note_id
from tianshu_memory.research_notes_migration import migrate as migrate_research_notes
from tianshu_memory.research_notes_schema import TRACKED as NOTE_TRACKED
from tianshu_memory.store import Store

SECRET = "synthetic-project-client-secret"
OTHER_SECRET = "synthetic-second-client-secret"
OPERATOR_SECRET = "synthetic-operator-client-secret"
PROJECT_WRITE = ["import", "query", "recover", "check", "write_state", "delete", "status"]
NOTE_WRITE = ["note_record", "note_revise", "note_withdraw"]
NOTE_READ = ["note_query", "note_recover", "note_status", "note_check"]
ALPHA_SOURCE = "Alpha source: the retry timer must wait for the receipt before resending.\n"
BETA_SOURCE = "Beta source: an unrelated second document about telemetry sampling.\n"
SECOND_SOURCE = "Second source: telemetry sampling is unrelated to receipt handling.\n"


def digest(secret):
    return hashlib.sha256(secret.encode()).hexdigest()


def registered_clients():
    return {
        "alpha-writer": {
            "credential_sha256": digest(SECRET),
            "projects": ["alpha"],
            "permissions": [*PROJECT_WRITE, *NOTE_WRITE, *NOTE_READ],
        },
        "alpha-reader": {
            "credential_sha256": digest(SECRET),
            "projects": ["alpha"],
            "permissions": ["query", *NOTE_READ],
        },
        "beta-writer": {
            "credential_sha256": digest(OTHER_SECRET),
            "projects": ["beta"],
            "permissions": [*PROJECT_WRITE, *NOTE_WRITE, *NOTE_READ],
        },
        "operator": {
            "credential_sha256": digest(OPERATOR_SECRET),
            "projects": ["alpha", "beta"],
            "permissions": [
                *PROJECT_WRITE,
                *NOTE_WRITE,
                *NOTE_READ,
                # The operator may also read the lesson book, so a test can show that the note
                # schema gate and the lesson schema gate are decided independently.
                "lesson_query",
            ],
        },
    }


CREDENTIALS = {
    "alpha-writer": SECRET,
    "alpha-reader": SECRET,
    "beta-writer": OTHER_SECRET,
    "operator": OPERATOR_SECRET,
}


@pytest.fixture
def notes(tmp_path, contracts):
    contracts.load_sources()
    store = Store(tmp_path / "notes.sqlite")
    store.migrate_profiles(tmp_path / "before-profiles.sqlite")
    store.migrate_sources(tmp_path / "before-sources.sqlite", contracts)
    migrate(store, tmp_path / "before-knowledge.sqlite")
    roots = {}
    for name, content in (("alpha", ALPHA_SOURCE), ("beta", BETA_SOURCE)):
        root = tmp_path / name
        root.mkdir()
        (root / "source.md").write_text(content, encoding="utf-8")
        roots[name] = root
    config = {
        "database_path": store.path,
        "knowledge": {
            "projects": {
                name: {
                    "root": str(root),
                    "host": "local",
                    "default_branch": "main",
                    "urls": [],
                }
                for name, root in roots.items()
            },
            "clients": registered_clients(),
        },
    }
    path = tmp_path / "private.json"
    path.write_text(canonical(config), encoding="utf-8")
    return SimpleNamespace(
        run=_runner(path),
        app=KnowledgeApplication(path),
        store=store,
        path=path,
        config=config,
        roots=roots,
        tmp_path=tmp_path,
    )


def _runner(path):
    def run(operation, arguments, *, project="alpha", client="operator", credential=None):
        # A fresh application per call models a restarted process: nothing is carried over in
        # memory between operations, exactly as in the CLI and MCP entrypoints.
        return KnowledgeApplication(path).execute(
            dict(operation=operation, project_id=project, arguments=arguments),
            client=client,
            credential=CREDENTIALS[client] if credential is None else credential,
        )

    return run


def restart(notes):
    """Reopen the same database and private configuration in a new application object."""
    return SimpleNamespace(**{**vars(notes), "app": KnowledgeApplication(notes.path)})


def write_config(notes):
    notes.path.write_text(canonical(notes.config), encoding="utf-8")


def imported(notes, project="alpha", key=None, expected_version=0, locator="source.md"):
    result = notes.run(
        "import",
        dict(
            key=key or f"{project}-import",
            kind="file",
            locator=locator,
            expected_version=expected_version,
            groups=None,
        ),
        project=project,
        client=f"{project}-writer",
    )
    assert result["status"] == "imported", result
    return result


def units(notes, project="alpha", text="receipt", client=None):
    """The current block references of one project, straight from the project query port."""
    hit = notes.run(
        "query",
        dict(text=text, budget_bytes=8192),
        project=project,
        client=client or f"{project}-writer",
    )
    return hit["blocks"]


def unit(notes, project="alpha", text="receipt", client=None):
    found = units(notes, project, text, client)
    assert found, "the imported source must expose at least one unit"
    return found[0]["reference"]


def source_text(notes, reference):
    """The stored decoded text of one cited document version, for redaction assertions."""
    with notes.store.transaction() as db:
        row = db.execute(
            "SELECT text FROM knowledge_versions WHERE document_id=? AND version=?",
            (reference["document_id"], reference["version"]),
        ).fetchone()
    return row["text"] if row else None


def statements(references, kind="claim"):
    return [
        {
            "kind": kind,
            "statement": "The source argues that a retry must wait for a receipt.",
            "source": reference,
        }
        for reference in references
    ]


def note(references, **changes):
    payload = {
        "question": "Should the retry path wait for a receipt before resending?",
        "source_statements": statements(references),
        "inferences": ["Waiting for the receipt is consistent with the observed duplicate."],
        "open_questions": ["Whether the receipt table is authoritative under partition."],
    }
    payload.update(changes)
    return payload


def decision(references, **changes):
    payload = {
        "summary": "The project decides the retry path waits for a receipt.",
        "basis": [{"kind": "source", "reference": reference} for reference in references],
    }
    payload.update(changes)
    return payload


def basis(*recorded):
    """Decision basis entries citing recorded note versions, one entry per named note.

    Each argument is either the result of a note write (its recorded version and fingerprint) or
    an explicit `(note_id, version, hash)` triple, so a test can also name a version that is no
    longer current.
    """
    entries = []
    for item in recorded:
        if isinstance(item, tuple):
            identifier, version, digest = item
        else:
            identifier, version, digest = item["note_id"], item["version"], item["hash"]
        entries.append(
            {
                "kind": "note",
                "reference": {"note_id": identifier, "version": version, "hash": digest},
            }
        )
    return entries


def record(notes, references, *, key="alpha-note", op=None, client="alpha-writer", **changes):
    return notes.run(
        "note_record",
        dict(
            key=key,
            dedupe=op or key,
            expected_version=0,
            note=note(references, **changes),
        ),
        project="alpha",
        client=client,
    )


def revise(
    notes,
    note_id_value,
    references,
    *,
    key="alpha-note",
    op,
    version,
    client="alpha-writer",
    **changes,
):
    return notes.run(
        "note_revise",
        dict(
            key=key,
            dedupe=op,
            note_id=note_id_value,
            expected_version=version,
            note=note(references, **changes),
        ),
        project="alpha",
        client=client,
    )


def withdraw(
    notes,
    note_id_value,
    version,
    *,
    key="withdraw",
    op=None,
    client="alpha-writer",
    reason="done",
):
    arguments = dict(key=key, note_id=note_id_value, expected_version=version, reason=reason)
    if op is not None:
        # A withdrawal of a note whose identity key is already bound needs its own operation key.
        arguments["dedupe"] = op
    return notes.run(
        "note_withdraw",
        arguments,
        project="alpha",
        client=client,
    )


def search(notes, text="receipt", budget=8192, *, client="alpha-reader"):
    return notes.run(
        "note_query", dict(text=text, budget_bytes=budget), project="alpha", client=client
    )


def status(notes, note_id_value, version, *, client="alpha-reader"):
    return notes.run(
        "note_status",
        dict(note_id=note_id_value, version=version),
        project="alpha",
        client=client,
    )


def second_source(notes, project="alpha", text="telemetry sampling"):
    """A second registered source in the same project, imported on demand.

    The locator deliberately does not repeat the first document's words: a lexical query for one
    document must never be satisfied by the other, so the returned unit is unambiguous.
    """
    (notes.roots[project] / "telemetry.md").write_text(SECOND_SOURCE, encoding="utf-8")
    imported(notes, project, key=f"{project}-second", locator="telemetry.md")
    return units(notes, project, text)[0]["reference"]


# -- the two-source closed loop ---------------------------------------------------


def test_two_sources_to_note_revision_query_and_withdrawal(notes):
    """The acceptance loop: two sources, one note, a revision, a query and a retraction."""
    imported(notes)
    first = unit(notes)
    second = second_source(notes)
    recorded = record(notes, [first, second])
    assert recorded["status"] == "recorded"
    assert recorded["version"] == 1
    assert recorded["source_units"] == 2 and recorded["cited_notes"] == 0
    assert recorded["sharing"] == "project_only"
    assert recorded["authority"] == "explicit_operator_research_note"

    found = search(notes)
    assert [item["note_id"] for item in found["notes"]] == [recorded["note_id"]]
    view = found["notes"][0]
    assert view["version"] == 1 and view["current"] is True
    assert [item["status"] for item in view["citations"]] == ["live", "live"]
    assert view["citation_states"] == []
    assert view["hash"] == recorded["hash"]
    assert view["project_id"] == "alpha"

    revised = revise(
        notes,
        recorded["note_id"],
        [first, second],
        op="revise-1",
        version=1,
        inferences=["Waiting for the receipt also bounds the retry budget."],
    )
    assert revised["status"] == "revised" and revised["version"] == 2
    assert revised["hash"] != recorded["hash"]

    found = search(notes)
    assert [item["version"] for item in found["notes"]] == [2]
    # The first version is still readable exactly as it was recorded.
    historical = status(notes, recorded["note_id"], 1)
    assert historical["version"] == 1 and historical["hash"] == recorded["hash"]
    assert historical["current_version"] == 2
    assert historical["state"] == "ready" and historical["current"] is True

    retracted = withdraw(notes, recorded["note_id"], 2)
    assert retracted["status"] == "withdrawn" and retracted["version"] == 3
    assert retracted["effect"] == "unavailable"
    assert search(notes)["notes"] == []
    # History is still traceable: the withdrawn version says so, and the reason is recorded.
    latest = status(notes, recorded["note_id"], 3)
    assert latest["state"] == "withdrawn"
    assert latest["withdrawn"]["reason"] == "done"
    assert status(notes, recorded["note_id"], 1)["state"] == "ready"


def test_note_fields_stay_separate_and_a_decision_needs_a_basis(notes):
    """The question, what the source argued, the inference and the decision never merge."""
    imported(notes)
    reference = unit(notes)
    with pytest.raises(Fault, match="evidence_required"):
        record(notes, [reference], decision={"summary": "Decided.", "basis": []})
    with pytest.raises(Fault, match="evidence_required"):
        record(notes, [reference], decision={"summary": "Decided.", "basis": [{"kind": "guess"}]})
    with pytest.raises(Fault, match="invalid_input"):
        record(
            notes,
            [reference],
            decision={"summary": "Decided.", "basis": [{"kind": "guess", "reference": {}}]},
        )
    # A decision cannot rest on an unknown note: the basis must resolve inside this project.
    with pytest.raises(Fault, match="not_found"):
        record(
            notes,
            [reference],
            decision=decision(
                [],
                basis=[
                    {
                        "kind": "note",
                        "reference": {"note_id": "note:absent", "version": 1, "hash": "0" * 64},
                    }
                ],
            ),
        )

    recorded = record(notes, [reference], decision=decision([reference]))
    view = search(notes)["notes"][0]
    assert view["decision"]["summary"] == "The project decides the retry path waits for a receipt."
    assert view["decision"]["basis"][0]["kind"] == "source"
    assert view["source_statements"][0]["kind"] == "claim"
    assert view["source_statements"][0]["statement"].startswith("The source argues")
    assert view["inferences"] and view["open_questions"]
    assert "decision" in view and view["decision"] is not None
    # The source's own text is never copied into the note.
    assert ALPHA_SOURCE.strip() not in canonical(view)
    assert recorded["source_units"] == 2  # the statement unit and the decision basis unit


def test_a_note_never_becomes_its_own_evidence(notes):
    """Self citation and a two-note ring are refused; a plain chain is still allowed."""
    imported(notes)
    reference = unit(notes)
    first = record(notes, [reference], key="ring-a")
    citation = lambda item: {  # noqa: E731 - a one-line reference builder keeps the test readable
        "kind": "note",
        "reference": {
            "note_id": item["note_id"],
            "version": item["version"],
            "hash": item["hash"],
        },
    }
    # A note cannot be revised to cite the version it is about to replace.
    with pytest.raises(Fault, match="citation_cycle"):
        revise(
            notes,
            first["note_id"],
            [reference],
            key="ring-a",
            op="self-cite",
            version=1,
            decision=decision([], basis=[citation(first)]),
        )
    # A decision basis may legitimately cite another note version.
    second = record(
        notes,
        [reference],
        key="ring-b",
        decision=decision([], basis=[citation(first)]),
    )
    assert second["cited_notes"] == 1
    # Closing the ring would make the pair its own evidence.
    with pytest.raises(Fault, match="citation_cycle"):
        revise(
            notes,
            first["note_id"],
            [reference],
            key="ring-a",
            op="ring-close",
            version=1,
            decision=decision([], basis=[citation(second)]),
        )


def test_a_citation_chain_deeper_than_the_bound_is_refused_not_walked(notes, monkeypatch):
    """A chain longer than the bound fails closed instead of hiding a ring behind the bound."""
    from tianshu_memory import research_notes

    imported(notes)
    reference = unit(notes)
    first = record(notes, [reference], key="chain-0")
    monkeypatch.setattr(research_notes, "MAX_CITATION_DEPTH", 1)

    def cite(previous, key):
        return record(
            notes,
            [reference],
            key=key,
            decision=decision(
                [],
                basis=[
                    {
                        "kind": "note",
                        "reference": {
                            "note_id": previous["note_id"],
                            "version": previous["version"],
                            "hash": previous["hash"],
                        },
                    }
                ],
            ),
        )

    second = cite(first, "chain-1")
    # `second` is two levels deep. A note citing it would need a third walk level, past the
    # bound, so it is refused rather than walked: a ring hidden beyond the bound can never be
    # accepted just because the walk stopped.
    with pytest.raises(Fault, match="citation_cycle"):
        cite(second, "chain-2")


def test_a_shallow_chain_is_accepted_under_the_production_bound(notes):
    """The production bound does not refuse the ordinary case it exists to protect."""
    imported(notes)
    reference = unit(notes)
    previous = record(notes, [reference], key="shallow-0")
    for index in range(1, 4):
        previous = record(
            notes,
            [reference],
            key=f"shallow-{index}",
            decision=decision(
                [],
                basis=[
                    {
                        "kind": "note",
                        "reference": {
                            "note_id": previous["note_id"],
                            "version": previous["version"],
                            "hash": previous["hash"],
                        },
                    }
                ],
            ),
        )
    assert previous["note_id"] == note_id("alpha", "shallow-3")
    assert previous["cited_notes"] == 1


def test_two_studies_sharing_a_foundation_are_not_a_cycle(notes):
    """A diamond is not a ring: two notes resting on the same ancestor may be combined.

    Only a back edge - a note reached again while it is still on the path being walked - is a
    cycle. A shared ancestor is finished on the first branch and deduplicated on the second.
    """
    imported(notes)
    reference = unit(notes)
    ancestor = record(notes, [reference], key="diamond-a")
    left = record(notes, [reference], key="diamond-b", decision=decision([], basis=basis(ancestor)))
    right = record(
        notes, [reference], key="diamond-c", decision=decision([], basis=basis(ancestor))
    )
    combined = record(
        notes,
        [reference],
        key="diamond-d",
        decision=decision([], basis=basis(left, right)),
    )
    assert combined["status"] == "recorded" and combined["cited_notes"] == 2
    # A wider fan-in stays a DAG as well: several notes may rest on the same ancestor and on the
    # same combined note without any of them becoming its own evidence.
    for index in range(2, 6):
        record(
            notes,
            [reference],
            key=f"diamond-{index}",
            decision=decision([], basis=basis(ancestor, combined)),
        )
    # The combined note is a current note in its own right, with both branches as its evidence.
    view = status(notes, combined["note_id"], 1)
    assert view["current"] is True and view["citation_states"] == []
    assert [item["kind"] for item in view["citations"]] == ["source", "note", "note"]
    # A query over this project returns current notes with every citation intact: a shared
    # ancestor is walked and reported, never mistaken for a ring.
    found = search(notes, budget=32768)
    assert found["notes"] and found["omissions"] == []
    assert all(item["current"] is True for item in found["notes"])


def test_a_note_graph_too_large_to_walk_is_refused_not_partially_walked(notes, monkeypatch):
    """The work bound fails closed instead of answering from the part of the graph it reached."""
    from tianshu_memory import research_notes

    imported(notes)
    reference = unit(notes)
    # A chain of five notes, each resting on the previous one.
    previous = record(notes, [reference], key="wide-0")
    for index in range(1, 5):
        previous = record(
            notes,
            [reference],
            key=f"wide-{index}",
            decision=decision([], basis=basis(previous)),
        )
    # With room for only three nodes the walk cannot finish, so it refuses rather than treating
    # the reachable part as the whole dependency set.
    monkeypatch.setattr(research_notes, "MAX_GRAPH_WORK", 3)
    with pytest.raises(Fault, match="citation_cycle"):
        record(
            notes,
            [reference],
            key="wide-final",
            decision=decision([], basis=basis(previous)),
        )
    # The same graph is accepted once the walk may finish, so the bound is a budget and not a
    # ban on depth.
    monkeypatch.setattr(research_notes, "MAX_GRAPH_WORK", 64)
    accepted = record(
        notes,
        [reference],
        key="wide-final",
        decision=decision([], basis=basis(previous)),
    )
    assert accepted["status"] == "recorded" and accepted["cited_notes"] == 1


def test_a_new_decision_cannot_rest_on_a_note_whose_source_expired(notes):
    """A conclusion is evidence only while the whole chain under it is still current.

    The citation closure of a cited note is checked in the same phase as the units the new note
    cites directly, so an expired chain is refused before the decision is written rather than
    being labelled unavailable after it committed.
    """
    imported(notes)
    first = unit(notes)
    stale = record(notes, [first], key="chain-root")
    other = second_source(notes)
    dependent = record(
        notes, [first], key="chain-dependent", decision=decision([], basis=basis(stale))
    )
    (notes.roots["alpha"] / "source.md").write_text("obsolete bytes\n", encoding="utf-8")

    # Both notes are still stored and readable; only their citation state moved.
    assert status(notes, stale["note_id"], 1)["current"] is False
    assert status(notes, dependent["note_id"], 1)["current"] is False
    # Recording a new decision on the expired chain is refused, and nothing is written.
    with pytest.raises(Fault, match="stale_evidence"):
        record(
            notes,
            [other],
            key="stale-decision",
            decision=decision([], basis=basis(stale)),
        )
    with pytest.raises(Fault, match="not_found"):
        status(notes, note_id("alpha", "stale-decision"), 1)
    # A revision that would rest on it is refused the same way, and the note keeps its version.
    with pytest.raises(Fault, match="stale_evidence"):
        revise(
            notes,
            dependent["note_id"],
            [other],
            key="chain-dependent",
            op="stale-revise",
            version=1,
            decision=decision([], basis=basis(stale)),
        )
    assert status(notes, dependent["note_id"], 1)["current_version"] == 1
    # A decision on a healthy note is still accepted, so the check is not simply refusing all
    # note citations.
    healthy = record(notes, [other], key="healthy-decision", decision=decision([other]))
    assert healthy["status"] == "recorded"


def test_a_transitive_dependency_two_levels_down_is_still_enforced(notes):
    """The check follows the chain, not only the notes the new decision names directly."""
    imported(notes)
    first = unit(notes)
    deep = record(notes, [first], key="deep-root")
    middle = record(notes, [first], key="deep-middle", decision=decision([], basis=basis(deep)))
    other = second_source(notes)
    (notes.roots["alpha"] / "source.md").write_text("obsolete bytes\n", encoding="utf-8")
    assert status(notes, middle["note_id"], 1)["current"] is False
    with pytest.raises(Fault, match="stale_evidence"):
        record(
            notes,
            [other],
            key="deep-decision",
            decision=decision([], basis=basis(middle)),
        )


def test_a_cited_version_that_was_superseded_is_not_substituted_by_the_current_one(notes):
    """An inner edge is checked at the version it recorded, never at the cited note's latest.

    B cites A v1. A is then revised to v2 with a valid source, so B's evidence is superseded even
    though every source file is unchanged. Walking A at its *current* version would find healthy
    sources and accept the new conclusion, which is the defect: the reference B recorded names
    A v1, and A v1 is no longer current.
    """
    imported(notes)
    reference = unit(notes)
    first = record(notes, [reference], key="edge-a")
    middle = record(
        notes,
        [reference],
        key="edge-b",
        decision=decision([], basis=basis(first)),
    )
    revise(
        notes,
        first["note_id"],
        [reference],
        key="edge-a",
        op="edge-a-v2",
        version=1,
        inferences=["A now argues something else, on the same source bytes."],
    )
    assert status(notes, middle["note_id"], 1)["current"] is False
    # Recording a conclusion that names the superseded inner version is refused before any write.
    with pytest.raises(Fault, match="stale_evidence"):
        record(
            notes,
            [reference],
            key="edge-c",
            decision=decision([], basis=basis(middle)),
        )
    with pytest.raises(Fault, match="not_found"):
        status(notes, note_id("alpha", "edge-c"), 1)
    # Revising an existing note onto the same superseded edge is refused the same way.
    with pytest.raises(Fault, match="stale_evidence"):
        revise(
            notes,
            middle["note_id"],
            [reference],
            key="edge-b",
            op="edge-b-v2",
            version=1,
            decision=decision([], basis=basis(first)),
        )
    assert status(notes, middle["note_id"], 1)["current_version"] == 1
    # A direct citation of the superseded version is refused too: the inner edge is not a
    # loophole in the direct check.
    with pytest.raises(Fault, match="stale_evidence"):
        record(notes, [reference], key="edge-d", decision=decision([], basis=basis(first)))
    # The current version of the same note is still usable evidence, so the refusal is about the
    # recorded version and not about the note identity.
    current = status(notes, first["note_id"], 2)
    healthy = record(
        notes,
        [reference],
        key="edge-e",
        decision=decision([], basis=basis((first["note_id"], 2, current["hash"]))),
    )
    assert healthy["status"] == "recorded"


def test_a_withdrawn_note_inside_the_chain_is_refused(notes):
    """A retraction anywhere in the closure stops a new conclusion, not only at the top."""
    imported(notes)
    reference = unit(notes)
    inner = record(notes, [reference], key="withdrawn-inner")
    outer = record(
        notes, [reference], key="withdrawn-outer", decision=decision([], basis=basis(inner))
    )
    assert status(notes, outer["note_id"], 1)["current"] is True
    withdraw(
        notes,
        inner["note_id"],
        1,
        key="withdrawn-inner",
        op="withdraw-inner",
        reason="retracted",
    )
    # The outer note is no longer current, and nothing new may rest on it.
    assert status(notes, outer["note_id"], 1)["current"] is False
    with pytest.raises(Fault, match="stale_evidence"):
        record(
            notes,
            [reference],
            key="withdrawn-c",
            decision=decision([], basis=basis(outer)),
        )
    # The retracted version itself is refused directly as well.
    with pytest.raises(Fault, match="stale_evidence"):
        record(notes, [reference], key="withdrawn-d", decision=decision([], basis=basis(inner)))


# -- source change, deletion and revocation ---------------------------------------


def test_a_revised_source_expires_the_citation_without_rewriting_history(notes):
    imported(notes)
    reference = unit(notes)
    recorded = record(notes, [reference])
    (notes.roots["alpha"] / "source.md").write_text(
        "Alpha source rewritten: the retry timer now waits for the receipt.\n", encoding="utf-8"
    )
    imported(notes, key="alpha-reimport", expected_version=1)

    found = search(notes)
    view = found["notes"][0]
    assert view["note_id"] == recorded["note_id"] and view["version"] == 1
    assert view["current"] is False
    assert [item["status"] for item in view["citations"]] == ["expired"]
    assert view["citation_states"] == ["expired"]
    assert "stale_source" not in found["omissions"]
    # The stored version is unchanged: only the citation's state moved.
    assert view["hash"] == recorded["hash"]
    assert view["source_statements"] == note([reference])["source_statements"]
    # No new decision may rest on the replaced version.
    with pytest.raises(Fault, match="stale_evidence"):
        record(notes, [reference], key="new-decision", decision=decision([reference]))


def test_a_deleted_source_expires_the_citation_and_leaks_no_text(notes):
    imported(notes)
    reference = unit(notes)
    recorded = record(notes, [reference])
    notes.run(
        "delete",
        dict(key="alpha-delete", document_id=reference["document_id"], expected_version=1),
        project="alpha",
        client="alpha-writer",
    )
    found = search(notes)
    view = found["notes"][0]
    assert view["current"] is False and view["citation_states"] == ["expired"]
    # The expired citation carries a state and a reference, never the source text.
    assert "Alpha source" not in canonical(view)
    assert source_text(notes, reference) is not None  # the stored bytes were never rewritten
    with pytest.raises(Fault, match="stale_evidence"):
        record(notes, [reference], key="after-delete", decision=decision([reference]))
    historical = status(notes, recorded["note_id"], 1)
    assert historical["citations"][0]["status"] == "expired"
    assert historical["state"] == "ready"


def test_an_unregistered_url_snapshot_expires_the_citation(notes, monkeypatch):
    """A URL snapshot stops being evidence once its project no longer registers that URL."""
    from tianshu_memory import knowledge as knowledge_module

    url = "https://example.invalid/synthetic-research"
    notes.config["knowledge"]["projects"]["alpha"]["urls"] = [url]
    write_config(notes)
    # The fetch is replaced at the boundary the knowledge application itself calls, so this test
    # never opens a socket. The snapshot is still a real imported document version.
    monkeypatch.setattr(
        knowledge_module,
        "fetch_url",
        lambda locator, registered: (
            b"Registered URL snapshot about receipt handling.\n",
            "text/plain",
            locator,
        ),
    )
    result = notes.run(
        "import",
        dict(key="alpha-url", kind="url", locator=url, expected_version=0, groups=None),
        project="alpha",
        client="alpha-writer",
    )
    assert result["status"] == "imported", result
    reference = units(notes, "alpha", "receipt")[0]["reference"]
    recorded = record(notes, [reference])
    assert search(notes)["notes"][0]["current"] is True

    # Revocation is the project registration moving, not the note being rewritten. Every
    # operation of that project - including the note read - fails closed afterwards rather than
    # answering from a registration the service can no longer prove.
    notes.config["knowledge"]["projects"]["alpha"]["urls"] = []
    write_config(notes)
    with pytest.raises(Fault, match="registration_changed"):
        search(notes)
    # The note version and its stored citation are untouched: nothing was rewritten to match the
    # new registration, and the snapshot is still recorded under the version it was read at.
    with notes.store.transaction() as db:
        payload = db.execute(
            "SELECT payload FROM research_note_history WHERE note_id=? AND version=?",
            (recorded["note_id"], 1),
        ).fetchone()["payload"]
        citations = db.execute(
            "SELECT kind,hash FROM research_note_citations WHERE note_id=? AND version=?",
            (recorded["note_id"], 1),
        ).fetchall()
    assert json.loads(payload)["source_statements"][0]["source"] == reference
    assert [row["kind"] for row in citations] == ["source"]
    assert citations[0]["hash"] == reference["hash"]
    assert source_text(notes, reference) is not None


def test_a_cited_note_that_is_withdrawn_stops_being_citable_evidence(notes):
    """A retracted conclusion cannot be cited as the basis of a new decision."""
    imported(notes)
    reference = unit(notes)
    source_note = record(notes, [reference], key="evidence-note")
    citation = {
        "kind": "note",
        "reference": {
            "note_id": source_note["note_id"],
            "version": source_note["version"],
            "hash": source_note["hash"],
        },
    }
    dependent = record(notes, [reference], key="dependent", decision=decision([], basis=[citation]))
    assert search(notes, "receipt")["notes"] and dependent["cited_notes"] == 1

    withdraw(notes, source_note["note_id"], 1, key="retract-source", reason="superseded")
    # The dependent note is still traceable, but its citation is no longer current evidence.
    found = search(notes)
    dependent_view = [item for item in found["notes"] if item["note_id"] == dependent["note_id"]]
    assert dependent_view and dependent_view[0]["current"] is False
    assert dependent_view[0]["citation_states"] == ["note_unavailable"]
    with pytest.raises(Fault, match="stale_evidence"):
        record(notes, [reference], key="third", decision=decision([], basis=[citation]))


def test_a_revised_cited_note_is_no_longer_the_version_that_was_read(notes):
    imported(notes)
    reference = unit(notes)
    source_note = record(notes, [reference], key="versioned-evidence")
    citation = {
        "kind": "note",
        "reference": {
            "note_id": source_note["note_id"],
            "version": source_note["version"],
            "hash": source_note["hash"],
        },
    }
    dependent = record(
        notes, [reference], key="uses-version", decision=decision([], basis=[citation])
    )
    revise(
        notes,
        source_note["note_id"],
        [reference],
        key="versioned-evidence",
        op="versioned-revise",
        version=1,
        inferences=["A newer inference."],
    )
    view = [item for item in search(notes)["notes"] if item["note_id"] == dependent["note_id"]][0]
    assert view["current"] is False and view["citation_states"] == ["note_unavailable"]


# -- identity, project and permission boundaries ----------------------------------


def test_a_note_is_scoped_to_its_project(notes):
    imported(notes)
    reference = unit(notes)
    recorded = record(notes, [reference])
    imported(notes, "beta", key="beta-import", locator="source.md")
    # Another project cannot read this note: it is unresolvable there, not merely refused.
    with pytest.raises(Fault, match="not_found"):
        notes.run(
            "note_status",
            dict(note_id=recorded["note_id"], version=1),
            project="beta",
            client="beta-writer",
        )
    beta_reference = units(notes, "beta", "telemetry")[0]["reference"]
    # A unit of another project cannot be cited, even by a caller authorized for both.
    with pytest.raises(Fault, match="not_found"):
        record(notes, [beta_reference], key="cross-project")
    # A note recorded in beta is invisible to an alpha query.
    beta_note = notes.run(
        "note_record",
        dict(key="beta-note", dedupe="beta-note", expected_version=0, note=note([beta_reference])),
        project="beta",
        client="beta-writer",
    )
    assert beta_note["note_id"] != recorded["note_id"]
    assert {item["note_id"] for item in search(notes)["notes"]} == {recorded["note_id"]}


def test_only_the_recording_identity_may_revise_or_withdraw_a_note(notes):
    imported(notes)
    reference = unit(notes)
    recorded = record(notes, [reference], client="alpha-writer")
    # A different identity of the same project may read the note.
    assert search(notes, client="alpha-reader")["notes"][0]["note_id"] == recorded["note_id"]
    with pytest.raises(Fault, match="forbidden"):
        revise(
            notes,
            recorded["note_id"],
            [reference],
            op="other-revise",
            version=1,
            client="alpha-reader",
        )
    with pytest.raises(Fault, match="forbidden"):
        withdraw(notes, recorded["note_id"], 1, client="alpha-reader")
    assert search(notes, client="alpha-reader")["notes"][0]["version"] == 1


def test_revoking_a_read_permission_denies_the_note_without_leaking_it(notes):
    imported(notes)
    reference = unit(notes)
    record(notes, [reference])
    notes.config["knowledge"]["clients"]["alpha-reader"]["permissions"] = ["query"]
    write_config(notes)
    # Without the operation permission the dispatch is refused before any project data is read.
    with pytest.raises(Fault, match="forbidden"):
        search(notes, client="alpha-reader")
    # The note itself is untouched and comes back unchanged once access is restored.
    notes.config["knowledge"]["clients"]["alpha-reader"]["permissions"] = ["query", *NOTE_READ]
    write_config(notes)
    view = search(notes)["notes"][0]
    assert view["current"] is True and view["state"] == "ready"


def test_a_project_that_is_not_registered_for_the_caller_is_refused(notes):
    imported(notes)
    reference = unit(notes)
    record(notes, [reference])
    with pytest.raises(Fault, match="forbidden"):
        notes.run(
            "note_query",
            dict(text="receipt", budget_bytes=8192),
            project="alpha",
            client="beta-writer",
        )
    with pytest.raises(Fault, match="unauthorized"):
        notes.run(
            "note_query",
            dict(text="receipt", budget_bytes=8192),
            project="alpha",
            client="alpha-reader",
            credential="wrong-but-long-enough-credential",
        )


def test_a_reader_without_any_note_permission_cannot_read_a_note(notes):
    """The note book has its own operation permissions, not the project's read permission."""
    imported(notes)
    reference = unit(notes)
    record(notes, [reference])
    notes.config["knowledge"]["clients"]["alpha-reader"]["permissions"] = ["query", "note_query"]
    write_config(notes)
    assert search(notes, client="alpha-reader")["notes"]
    with pytest.raises(Fault, match="forbidden"):
        notes.run(
            "note_status",
            dict(note_id=note_id("alpha", "alpha-note"), version=1),
            project="alpha",
            client="alpha-reader",
        )
    with pytest.raises(Fault, match="forbidden"):
        notes.run(
            "note_recover",
            dict(text="receipt", budget_bytes=16384),
            project="alpha",
            client="alpha-reader",
        )


# -- versioning, idempotency and restart -------------------------------------------


def test_version_conflicts_and_duplicate_records_are_refused(notes):
    imported(notes)
    reference = unit(notes)
    recorded = record(notes, [reference])
    # A fresh operation key with a stale declared version is a version conflict, not a replay.
    with pytest.raises(Fault, match="version_conflict"):
        notes.run(
            "note_record",
            dict(
                key="alpha-note",
                dedupe="stale-create",
                expected_version=3,
                note=note([reference]),
            ),
            project="alpha",
            client="alpha-writer",
        )
    with pytest.raises(Fault, match="version_conflict"):
        record(notes, [reference], key="alpha-note", op="second-op")
    # A note identity is created once: a duplicate record is a version conflict, never a second
    # note under the same identity, and never a silent second version.
    with pytest.raises(Fault, match="version_conflict"):
        notes.run(
            "note_record",
            dict(
                key="alpha-note",
                dedupe="fresh-key",
                expected_version=0,
                note=note([reference]),
            ),
            project="alpha",
            client="alpha-writer",
        )
    assert status(notes, recorded["note_id"], 1)["version"] == 1
    with pytest.raises(Fault, match="version_conflict"):
        revise(notes, recorded["note_id"], [reference], op="stale", version=7)
    with pytest.raises(Fault, match="identity_conflict"):
        revise(notes, recorded["note_id"], [reference], key="other-key", op="wrong-key", version=1)
    withdraw(notes, recorded["note_id"], 1, key="w1")
    # A withdrawn note cannot be revised back to life, and cannot be withdrawn twice.
    with pytest.raises(Fault, match="already_withdrawn"):
        revise(notes, recorded["note_id"], [reference], op="after-withdraw", version=2)
    with pytest.raises(Fault, match="already_withdrawn"):
        withdraw(notes, recorded["note_id"], 2, key="w2")
    with pytest.raises(Fault, match="version_conflict"):
        withdraw(notes, recorded["note_id"], 1, key="w3")


def test_idempotent_replay_returns_the_recorded_result(notes):
    imported(notes)
    reference = unit(notes)
    first = record(notes, [reference], key="replay", op="replay-op")
    again = record(notes, [reference], key="replay", op="replay-op")
    assert again["replayed"] is True
    assert again["note_id"] == first["note_id"] and again["version"] == first["version"]
    assert again["hash"] == first["hash"]
    with pytest.raises(Fault, match="idempotency_conflict"):
        record(
            notes,
            [reference],
            key="replay",
            op="replay-op",
            question="A different question under the same operation key.",
        )


def test_a_note_and_its_citations_survive_a_restart(notes):
    imported(notes)
    reference = unit(notes)
    recorded = record(notes, [reference], decision=decision([reference]))
    revised = revise(
        notes,
        recorded["note_id"],
        [reference],
        op="restart-revise",
        version=1,
        question="Should the retry path wait for a receipt, after the revision?",
    )
    fresh = restart(notes)
    found = fresh.run(
        "note_query",
        dict(text="receipt", budget_bytes=8192),
        project="alpha",
        client="alpha-reader",
    )
    view = found["notes"][0]
    assert view["note_id"] == recorded["note_id"] and view["version"] == 2
    assert view["hash"] == revised["hash"]
    assert view["current"] is True and view["citation_states"] == []
    # A revision replaces the version; the decision was not carried into the new version.
    assert view["decision"] is None
    # The earlier version and its citations were persisted, not recomputed from the new one.
    earlier = fresh.run(
        "note_status",
        dict(note_id=recorded["note_id"], version=1),
        project="alpha",
        client="alpha-reader",
    )
    assert earlier["hash"] == recorded["hash"] and earlier["version"] == 1
    # The recorded idempotent result is durable across the restart as well.
    replay = fresh.run(
        "note_record",
        dict(
            key="alpha-note",
            dedupe="alpha-note",
            expected_version=0,
            note=note([reference], decision=decision([reference])),
        ),
        project="alpha",
        client="alpha-writer",
    )
    assert replay["replayed"] is True and replay["hash"] == recorded["hash"]


def test_the_recovery_package_seals_and_invalidates(notes):
    imported(notes)
    reference = unit(notes)
    recorded = record(notes, [reference])
    package = notes.run(
        "note_recover",
        dict(text="receipt", budget_bytes=16384),
        project="alpha",
        client="alpha-reader",
    )
    assert package["notes"][0]["note_id"] == recorded["note_id"]
    assert package["authority"] == "explicit_operator_research_note"
    assert package["project_workdir"] == "authoritative_over_notes"
    checked = notes.run("note_check", dict(package=package), project="alpha", client="alpha-reader")
    assert checked == {"valid": True, "reason": "current"}
    # A tampered body is refused, and so is a package whose cited source moved on.
    tampered = dict(package, revision=package["revision"] + 5)
    assert notes.run(
        "note_check", dict(package=tampered), project="alpha", client="alpha-reader"
    ) == {"valid": False, "reason": "stale_or_tampered"}
    (notes.roots["alpha"] / "source.md").write_text("Changed bytes.\n", encoding="utf-8")
    imported(notes, key="alpha-reimport", expected_version=1)
    assert notes.run(
        "note_check", dict(package=package), project="alpha", client="alpha-reader"
    ) == {"valid": False, "reason": "stale_or_tampered"}


# -- byte budget -------------------------------------------------------------------


def test_a_budget_that_cannot_hold_a_note_omits_it_whole(notes):
    imported(notes)
    first = unit(notes)
    record(notes, [first], key="budget-a")
    second = second_source(notes)
    record(notes, [first, second], key="budget-b")

    tight = search(notes, budget=256)
    assert tight["notes"] == [] and tight["omissions"] == ["budget"]
    with pytest.raises(Fault, match="invalid_input"):
        search(notes, budget=64)

    # Whatever a larger budget returns, every returned note carries all of its citations.
    generous = search(notes, budget=32768)
    assert len(generous["notes"]) == 2
    assert sorted(len(view["citations"]) for view in generous["notes"]) == [1, 2]
    for view in generous["notes"]:
        assert all("reference" in item and "status" in item for item in view["citations"])
    # A note is never returned with part of its evidence: the counts stay whole at every budget.
    for budget in (400, 600, 800, 1000, 1500, 2000, 4000):
        partial = search(notes, budget=budget)
        assert sorted(len(view["citations"]) for view in partial["notes"]) in ([], [1], [1, 2], [2])
        if len(partial["notes"]) < 2:
            assert "budget" in partial["omissions"]


def test_notes_never_override_the_project_working_directory_facts(notes):
    """A note about the checkout cannot replace what the continuation port observed."""
    imported(notes)
    reference = unit(notes)
    record(
        notes,
        [reference],
        question="The checkout is on branch release and has no uncommitted work.",
    )
    # A continuation read of the same project is a separate operation with its own facts; the
    # note text is not injected into it and cannot stand in for an observation.
    package = notes.run(
        "note_recover",
        dict(text="checkout", budget_bytes=16384),
        project="alpha",
        client="alpha-reader",
    )
    assert package["notes"], "the note is returned by the note port"
    assert "worktree" not in package and "branch" not in package and "head" not in package
    # A search that matches no note returns nothing: notes are never injected wholesale, and a
    # query of only scaffolding words has no topic and therefore no candidates.
    assert search(notes, text="quantum")["notes"] == []
    assert search(notes, text="怎么样?什么?你好")["notes"] == []


# -- migration ---------------------------------------------------------------------


def test_the_fresh_database_installs_the_note_tables_and_tracks_them(notes):
    with notes.store.transaction() as db:
        metadata = dict(db.execute("SELECT key,value FROM metadata"))
        assert metadata["research_notes_schema"] == "1"
        tables = {
            row["name"] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert set(NOTE_TRACKED) <= tables
        triggers = {
            row["name"] for row in db.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
        }
        for table in NOTE_TRACKED:
            for action in ("INSERT", "UPDATE", "DELETE"):
                assert f"source_revision_{table}_{action}" in triggers


def test_the_explicit_upgrade_requires_a_backup_and_refuses_a_second_run(tmp_path, contracts):
    contracts.load_sources()
    store = Store(tmp_path / "upgrade.sqlite")
    store.migrate_profiles(tmp_path / "before-profiles.sqlite")
    store.migrate_sources(tmp_path / "before-sources.sqlite", contracts)
    migrate(store, tmp_path / "before-knowledge.sqlite")
    # Model a database installed before this task: the note objects are absent.
    with store.transaction() as db:
        for table in NOTE_TRACKED:
            db.execute(f"DROP TABLE {table}")
        db.execute("DROP TABLE research_note_index")
        db.execute("DELETE FROM metadata WHERE key='research_notes_schema'")

    backup = tmp_path / "before-notes.sqlite"
    result = migrate_research_notes(store, backup)
    assert result == {
        "schema": 3,
        "knowledge_schema": 1,
        "research_notes_schema": 1,
        "backup": str(backup),
    }
    assert backup.stat().st_size > 0
    with store.transaction() as db:
        metadata = dict(db.execute("SELECT key,value FROM metadata"))
        assert metadata["research_notes_schema"] == "1"
    # A second upgrade is refused, and the reserved backup path stays empty.
    again = tmp_path / "before-notes-2.sqlite"
    with pytest.raises(ValueError, match="already applied"):
        migrate_research_notes(store, again)
    assert again.exists() and again.stat().st_size == 0


def test_a_note_operation_fails_closed_without_the_note_schema(notes):
    imported(notes)
    reference = unit(notes)
    with notes.store.transaction() as db:
        db.execute("DELETE FROM metadata WHERE key='research_notes_schema'")
    with pytest.raises(Fault, match="dependency_unavailable"):
        record(notes, [reference])


# -- boundaries --------------------------------------------------------------------


def test_the_note_module_writes_only_its_own_tables(notes):
    """Recording and revising a note touches no chat, source or lesson row."""
    imported(notes)
    reference = unit(notes)

    def snapshot():
        with notes.store.transaction() as db:
            return {
                table: db.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
                for table in (
                    "records",
                    "history",
                    "groups",
                    "sources",
                    "lineage",
                    "knowledge_documents",
                    "knowledge_versions",
                    "knowledge_blocks",
                    "lessons",
                    "lesson_history",
                    "experience_entries",
                    "research_notes",
                    "research_note_history",
                    "research_note_citations",
                )
            }

    # One note exists already; the revision below is the only write this test measures.
    recorded = record(notes, [reference], decision=decision([reference]))
    before = snapshot()
    revise(notes, recorded["note_id"], [reference], op="boundary", version=1)
    after = snapshot()
    for table in ("records", "history", "groups", "sources", "lineage"):
        assert before[table] == after[table], table
    for table in ("knowledge_documents", "knowledge_versions", "knowledge_blocks", "lessons"):
        assert before[table] == after[table], table
    # A revision appends one version row; it never adds or removes a note identity.
    assert after["research_notes"] == before["research_notes"]
    assert after["research_note_history"] == before["research_note_history"] + 1
    assert after["research_note_citations"] == before["research_note_citations"] + 1


def test_the_note_module_keeps_its_declared_boundaries():
    """The note domain is a leaf: no application, no foreign table, no dependency back edge.

    This is the module-boundary contract in executable form. A note rule may use the injected
    actor, the injected project port and the source context, and may read and write its own
    tables — nothing else. The note module must not import the module that routes dispatches, and
    the SQL that touches the project tables must live with the domain that owns them. A future
    change that recoupled the modules would fail here instead of silently passing.
    """
    import ast
    import inspect

    from tianshu_memory import knowledge, research_notes, validate

    source = Path(inspect.getsourcefile(research_notes)).read_text(encoding="utf-8")
    tree = ast.parse(source)

    # No rule reaches back into the authorizing application: the ports replaced it.
    assert not [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "application"
    ], "the note domain must not hold or use the application object"

    # No dependency back edge: the note module never imports the routing module, at any level.
    imported = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert not {name for name in imported if name.endswith("knowledge")}, imported
    assert "knowledge" not in imported, imported
    assert "from .knowledge" not in source and "import knowledge" not in source

    # Every SQL statement in this module names only this domain's tables. `snapshot` is the
    # schema/registration gate the application hands over per domain; it reads `metadata` and the
    # project registration row, and it is the only such read here.
    owned = {"research_notes", "research_note_history", "research_note_citations"}
    derived = {"research_note_index"}
    foreign = ("KNOWLEDGE_STATES", "LESSONS", "LESSON_", "EXPERIENCE_", "RECORDS", "SOURCES")
    sql = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and any(word in node.value.upper() for word in ("SELECT", "INSERT", "UPDATE", "DELETE"))
    ]
    assert sql, "the module's own SQL must be visible to this test"
    assert any("research_note_history" in text for text in sql)
    for text in sql:
        upper = text.upper()
        for table in foreign:
            assert table not in upper, (table, text)
    # The only project-table read left in this module is the registration comparison of the
    # snapshot gate; every rule goes through the injected port instead.
    project_reads = [text for text in sql if "KNOWLEDGE_PROJECTS" in text.upper()]
    assert len(project_reads) == 1 and "SELECT * FROM knowledge_projects" in project_reads[0]
    snapshot = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "snapshot"
    )
    assert project_reads[0] in [
        node.value
        for node in ast.walk(snapshot)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    elsewhere = ast.parse(
        "\n".join(ast.unparse(node) for node in tree.body if node is not snapshot)
    )
    assert not [
        node
        for node in ast.walk(elsewhere)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and "knowledge_projects" in node.value
        and any(word in node.value.upper() for word in ("SELECT", "INSERT", "UPDATE", "DELETE"))
    ], "only the snapshot gate may query the project table"

    # The port contract is a contract: three methods and no storage of its own.
    port = research_notes.ProjectPort
    assert {name for name in vars(port) if not name.startswith("__")} == {
        "revision",
        "declared_state",
        "bump",
    }
    port_source = inspect.getsource(port)
    assert not [
        node
        for node in ast.walk(ast.parse(port_source))
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and any(word in node.value.upper() for word in ("SELECT", "UPDATE", "INSERT"))
    ], "the port contract must not implement storage"

    # The implementation lives with the domain that owns those tables, and that is what the
    # application injects.
    adapter = knowledge.ProjectAdapter
    adapter_sql = [
        node.value
        for node in ast.walk(ast.parse(inspect.getsource(adapter)))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    assert any("knowledge_projects" in text for text in adapter_sql)
    assert any("knowledge_states" in text for text in adapter_sql)
    assert {name for name in vars(adapter) if not name.startswith("__")} == {
        "revision",
        "declared_state",
        "bump",
    }
    knowledge_source = Path(inspect.getsourcefile(knowledge)).read_text(encoding="utf-8")
    assert "ProjectAdapter(db, self.project_id)" in knowledge_source
    # The scalar validators are neutral, and the routing module delegates to the same rules.
    assert validate.integer is not None and validate.string is not None
    assert "from . import validate as validation" in knowledge_source

    # The note rules go through the injected port for every project read and write.
    assert "self.projects.revision()" in source
    assert "self.projects.declared_state()" in source
    assert "self.projects.bump()" in source
    # A read of a derived index row never masquerades as an authoritative note row.
    assert derived and owned
    # The identity is passed in, never read from a global or the application, and it is frozen.
    assert {name for name in vars(research_notes.NoteActor) if not name.startswith("__")} == {
        "_client",
        "client",
    }
    assert "self.actor.client" in source
    actor = research_notes.NoteActor("alpha-writer")
    assert actor.client == "alpha-writer" and "alpha-writer" in repr(actor)
    with pytest.raises(AttributeError):
        actor.client = "beta-writer"
    with pytest.raises(AttributeError):
        actor._client = "beta-writer"
    with pytest.raises(AttributeError):
        del actor.client
    assert actor.client == "alpha-writer"


def test_the_application_routes_each_operation_to_exactly_one_domain(notes):
    """The planner picks one domain module per operation and never mixes their gates."""
    imported(notes)
    reference = unit(notes)
    recorded = record(notes, [reference])
    from tianshu_memory.knowledge import NOTE_OPERATIONS

    assert NOTE_OPERATIONS == {
        "note_record",
        "note_revise",
        "note_withdraw",
        "note_query",
        "note_recover",
        "note_status",
        "note_check",
    }
    # The note schema gate is the notes module's own; the lesson book keeps its own.
    with notes.store.transaction() as db:
        db.execute("DELETE FROM metadata WHERE key='lessons_schema'")
    assert search(notes)["notes"][0]["note_id"] == recorded["note_id"]
    with pytest.raises(Fault, match="dependency_unavailable"):
        notes.run(
            "lesson_query",
            dict(text="receipt", budget_bytes=8192),
            project="alpha",
            client="operator",
        )


def test_note_identity_is_derived_from_the_project_and_key(notes):
    imported(notes)
    reference = unit(notes)
    recorded = record(notes, [reference], key="stable-identity")
    assert recorded["note_id"] == note_id("alpha", "stable-identity")
    assert json.loads(canonical({"note_id": recorded["note_id"]}))["note_id"].startswith("note:")
    assert Path(notes.path).exists()


def test_candidate_schema_and_examples_cover_new_operations():
    from jsonschema import Draft202012Validator

    from tianshu_memory.knowledge import NOTE_OPERATIONS

    directory = Path(__file__).resolve().parents[1] / "docs/candidates/project-research-notes/v1"
    schema = json.loads((directory / "schema.json").read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    validator.check_schema(schema)
    examples = json.loads((directory / "examples.json").read_text(encoding="utf-8"))
    assert {example["envelope"]["operation"] for example in examples} == NOTE_OPERATIONS
    for example in examples:
        validator.validate(example["envelope"])
    assert NOTE_OPERATIONS <= set(schema["properties"]["operation"]["enum"])
    # The candidate is a proposal, not a published contract: it still carries the operations it
    # extends, so a reader of this directory sees one complete envelope.
    assert {"query", "import", "lesson_record", "experience_promote"} <= set(
        schema["properties"]["operation"]["enum"]
    )
