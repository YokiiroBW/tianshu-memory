"""Readiness and liveness: every check observes, and not one of them changes anything.

The central claim of this file is a negative one — that asking a process whether it is ready does
not alter the thing it is being asked about. So the tests snapshot the filesystem and the database
byte for byte around a probe, and they check the database's own journal and lock state, rather than
trusting the code to be read-only by inspection.
"""

import hashlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from tianshu_memory import source_recovery
from tianshu_memory.contracts import Contracts
from tianshu_memory.diagnostics import (
    CHAT_SERVICE,
    CHECKS_BY_SERVICE,
    KNOWLEDGE_SERVICE,
    Diagnostics,
)
from tianshu_memory.domain import canonical
from tianshu_memory.knowledge_migration import migrate as migrate_knowledge
from tianshu_memory.runtime_probes import (
    BLOCKING_STATES,
    NON_DURABLE,
    NOT_CONFIGURED,
    NOT_VERIFIED,
    OK,
    ProbeConfig,
    assess,
    keys_for,
    present_token,
    probe_configuration,
    probe_contract,
    probe_database,
    probe_guard,
    probe_installed,
    probe_knowledge_extensions,
    probe_log,
    probe_mode,
    probe_ownership,
    readiness,
)
from tianshu_memory.store import Store

CLIENT = "alpha-writer"
DIGEST = hashlib.sha256(b"synthetic-credential").hexdigest()


def workspace_contracts():
    root = Path(__file__).resolve().parents[1]
    context = json.loads((root / ".runtime/workspace-context.json").read_text(encoding="utf-8"))
    return Path(context["workspace"]) / "contracts/text-dialogue/v1"


@pytest.fixture(scope="module")
def contracts():
    return Contracts(workspace_contracts())


@pytest.fixture
def chat_service(tmp_path, contracts):
    """A real schema 3 database with its independent guard, and the private configuration for it."""
    store = Store(tmp_path / "memory.sqlite")
    store.migrate_profiles(tmp_path / "before-profiles.sqlite")
    store.migrate_sources(tmp_path / "before-sources.sqlite", contracts)
    config = {
        "mode": "source_sync",
        "database_path": str(store.path),
        "contract_directory": str(contracts.directory),
        "source_sync": {"recovery_path": str(store.recovery_path)},
    }
    path = tmp_path / "chat.json"
    path.write_text(canonical(config), encoding="utf-8")
    return SimpleNamespace(store=store, config=config, path=path, tmp_path=tmp_path)


@pytest.fixture
def knowledge_service(tmp_path, contracts):
    """A real project-knowledge database with its registered client, and its configuration."""
    store = Store(tmp_path / "notes.sqlite")
    store.migrate_profiles(tmp_path / "before-profiles.sqlite")
    store.migrate_sources(tmp_path / "before-sources.sqlite", contracts)
    migrate_knowledge(store, tmp_path / "before-knowledge.sqlite")
    root = tmp_path / "alpha"
    root.mkdir()
    config = {
        "database_path": str(store.path),
        "knowledge": {
            "projects": {
                "alpha": {
                    "root": str(root),
                    "host": "local",
                    "default_branch": "main",
                    "urls": [],
                }
            },
            "clients": {
                CLIENT: {
                    "credential_sha256": DIGEST,
                    "projects": ["alpha"],
                    "permissions": ["query"],
                }
            },
        },
    }
    path = tmp_path / "knowledge.json"
    path.write_text(canonical(config), encoding="utf-8")
    return SimpleNamespace(store=store, config=config, path=path, tmp_path=tmp_path)


@pytest.fixture
def diagnostics(tmp_path):
    directory = tmp_path / "logs"
    directory.mkdir()
    return Diagnostics(CHAT_SERVICE, {"log_directory": str(directory)})


def settings_for(service, subject, diagnostics, *, contract_path, client=None, handles=None):
    return ProbeConfig(
        service=service,
        diagnostics=diagnostics,
        config_path=subject.path,
        contract_path=contract_path,
        runtime=SimpleNamespace(active=True, released=False),
        client=client,
        handles=handles,
    )


def diagnostics_package():
    from tianshu_memory.server_runtime import workspace_diagnostics_path

    root = Path(__file__).resolve().parents[1]
    directory = workspace_diagnostics_path(root / "pyproject.toml")
    if directory is None or not (directory / "manifest.json").is_file():
        pytest.skip("the published diagnostics package is not reachable from this checkout")
    return directory


def fingerprint(paths):
    """Every byte of every named file, so a probe that wrote anything cannot pass unnoticed."""
    state = {}
    for path in paths:
        path = Path(path)
        state[str(path)] = path.read_bytes() if path.is_file() else None
    return state


def directory_listing(directory):
    return sorted(entry.name for entry in Path(directory).iterdir())


def listing_without_sidecars(directory):
    """The directory's own contents, excluding the transient SQLite WAL sidecars.

    SQLite creates an empty write-ahead log whenever any connection — including the read-only one a
    probe opens — is open to a WAL-mode database, and removes it again at the last close. Their
    presence around a snapshot therefore says nothing about whether anything was written; what must
    not change is the set of files that hold state.
    """
    return sorted(
        name for name in directory_listing(directory) if not name.endswith(("-wal", "-shm"))
    )


def settle(database):
    """Fold the write-ahead log back into the database file, as a stopped writer would.

    The fixture migrated the database, so its journal mode is WAL and the `-wal`/`-shm` sidecars are
    transient: SQLite creates and removes them as connections come and go. Settling first is what
    makes "the probe changed nothing" a statement about the probe rather than about which sidecar
    happened to exist when the snapshot was taken.
    """
    connection = sqlite3.connect(str(database))
    try:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        connection.close()


def test_a_healthy_chat_process_reports_ready(chat_service, diagnostics):
    settings = settings_for(
        CHAT_SERVICE, chat_service, diagnostics, contract_path=diagnostics_package()
    )
    document = readiness(settings)
    assert document["status"] == "ready"
    assert document["service"] == "memory"
    assert set(document["checks"]) == set(CHECKS_BY_SERVICE[CHAT_SERVICE])
    assert set(document) == {"status", "service", "checks"}
    # Every local check verified. The one exception is the remote dependency, which this process
    # deliberately never calls, so it is reported untested rather than claimed reachable.
    assert document["checks"]["remote"] == "not_verified"
    assert all(state == "ok" for key, state in document["checks"].items() if key != "remote")
    # And the remote state is genuinely non-blocking rather than accidentally tolerated.
    assert "not_verified" not in BLOCKING_STATES["remote"]


def test_a_healthy_knowledge_process_reports_ready(knowledge_service, diagnostics):
    diagnostics = Diagnostics(KNOWLEDGE_SERVICE, {"log_directory": str(diagnostics.sink.directory)})
    settings = settings_for(
        KNOWLEDGE_SERVICE,
        knowledge_service,
        diagnostics,
        contract_path=diagnostics_package(),
        client=CLIENT,
    )
    document = readiness(settings)
    assert document["status"] == "ready"
    assert document["service"] == "memory-knowledge"
    assert set(document["checks"]) == set(CHECKS_BY_SERVICE[KNOWLEDGE_SERVICE])


def test_the_knowledge_service_depends_on_the_same_checkpoint_its_store_verifies(
    knowledge_service, diagnostics
):
    """The reviewed defect: deleting the guard left readiness claiming `ready`.

    The knowledge entry builds its Store with this exact recovery path and every write it performs
    re-verifies that checkpoint first, so a process whose guard is gone cannot serve at all. A
    readiness verdict has to say so, and it has to say so without touching the file: a probe that
    rebuilt a guard would be repairing the very protection it is reporting on.
    """
    diagnostics = Diagnostics(KNOWLEDGE_SERVICE, {"log_directory": str(diagnostics.sink.directory)})
    guard = Path(str(knowledge_service.store.path) + ".source-guard.json")
    assert guard.is_file(), "a migrated knowledge database records a checkpoint"
    before = guard.read_bytes()

    def verdict():
        return readiness(
            settings_for(
                KNOWLEDGE_SERVICE,
                knowledge_service,
                diagnostics,
                contract_path=diagnostics_package(),
                client=CLIENT,
            )
        )

    assert verdict()["checks"]["database"] == "ok"
    guard.unlink()
    absent = verdict()
    assert absent["status"] == "not_ready"
    assert absent["checks"]["database"] == "not_configured"
    # Nothing was re-created, and nothing was written in its place.
    assert not guard.exists()
    guard.write_bytes(before)
    assert verdict()["status"] == "ready"
    # A guard that no longer agrees with the database is not-ready either, and is left alone: the
    # checkpoint is compared as the same canonical form the Store compares, not re-derived here.
    damaged_value = json.loads(before)
    damaged_value["revision"] = damaged_value["revision"] + 1
    damaged = canonical(damaged_value).encode("utf-8")
    guard.write_bytes(damaged)
    disagreeing = verdict()
    assert disagreeing["status"] == "not_ready"
    assert disagreeing["checks"]["database"] == "failed"
    assert guard.read_bytes() == damaged
    guard.write_bytes(before)


def test_the_knowledge_guard_is_the_one_the_configuration_names(knowledge_service, diagnostics):
    """A configured recovery path is the guard that counts, exactly as the Store reads it."""
    elsewhere = knowledge_service.tmp_path / "placed-elsewhere.json"
    assert not elsewhere.exists()
    knowledge_service.config["source_sync"] = {"recovery_path": str(elsewhere)}
    knowledge_service.path.write_text(canonical(knowledge_service.config), encoding="utf-8")
    diagnostics = Diagnostics(KNOWLEDGE_SERVICE, {"log_directory": str(diagnostics.sink.directory)})
    document = readiness(
        settings_for(
            KNOWLEDGE_SERVICE,
            knowledge_service,
            diagnostics,
            contract_path=diagnostics_package(),
            client=CLIENT,
        )
    )
    # The default guard beside the database is present but is not the one this configuration uses.
    assert Path(str(knowledge_service.store.path) + ".source-guard.json").is_file()
    assert document["status"] == "not_ready"
    assert document["checks"]["database"] == "not_configured"
    assert not elsewhere.exists()


def test_the_document_has_exactly_the_three_contract_fields(chat_service, diagnostics):
    settings = settings_for(
        CHAT_SERVICE, chat_service, diagnostics, contract_path=diagnostics_package()
    )
    document = readiness(settings)
    assert list(document) == ["status", "service", "checks"]
    assert document["status"] in {"ready", "not_ready"}
    for state in document["checks"].values():
        assert state in {"ok", "failed", "not_configured", "not_verified", "non_durable"}


def test_the_readiness_document_never_carries_a_path_a_project_or_a_count(
    chat_service, diagnostics
):
    """Nothing about the configuration, the data or a path can leak out of a probe."""
    settings = settings_for(
        CHAT_SERVICE, chat_service, diagnostics, contract_path=diagnostics_package()
    )
    text = json.dumps(readiness(settings))
    assert str(chat_service.tmp_path) not in text
    assert str(chat_service.store.path) not in text
    assert "sqlite" not in text.lower()
    assert "alpha" not in text
    assert document_keys(readiness(settings)) == ["status", "service", "checks"]


def document_keys(document):
    return list(document)


def test_a_remote_dependency_is_never_claimed_as_verified(chat_service, diagnostics):
    """An untested dependency is reported as untested — and that is not, by itself, a failure."""
    settings = settings_for(
        CHAT_SERVICE, chat_service, diagnostics, contract_path=diagnostics_package()
    )
    document = readiness(settings)
    assert document["checks"]["remote"] == "not_verified"
    assert NOT_VERIFIED.state == "not_verified"
    # The contract requires `not_verified` here rather than a success one local read could never
    # establish, and readiness is decided by the local conditions it names.
    assert document["status"] == "ready"


def test_the_probes_leave_the_database_the_guard_and_the_directory_untouched(
    chat_service, diagnostics
):
    """The whole point of this file: observing changes nothing, verified byte for byte.

    The `-wal` and `-shm` sidecars are deliberately not part of this assertion. SQLite creates an
    empty write-ahead log while any connection — including a read-only one — is open to a WAL-mode
    database, and removes it again at the last close, so their existence around a snapshot says
    nothing about whether anything was written. What must not change is the database file itself and
    the independent guard, and the directory's own contents must not gain or lose an entry.
    """
    database = Path(chat_service.store.path)
    settle(database)
    durable = [database, Path(chat_service.store.recovery_path)]
    before = fingerprint(durable)
    listing = listing_without_sidecars(chat_service.tmp_path)
    settings = settings_for(
        CHAT_SERVICE, chat_service, diagnostics, contract_path=diagnostics_package()
    )
    for _ in range(3):
        assert readiness(settings)["status"] == "ready"
    assert fingerprint(durable) == before
    assert listing_without_sidecars(chat_service.tmp_path) == listing
    # And after the probe's own connections are gone, the files are still exactly as they were.
    settle(database)
    assert fingerprint(durable) == before


def test_an_absent_database_stays_absent(tmp_path, diagnostics):
    """`mode=ro` is what makes absent mean absent: no empty database is created."""
    config = {"mode": "source_sync", "database_path": str(tmp_path / "never.sqlite")}
    path = tmp_path / "chat.json"
    path.write_text(canonical(config), encoding="utf-8")
    subject = SimpleNamespace(path=path, config=config, tmp_path=tmp_path)
    settings = settings_for(CHAT_SERVICE, subject, diagnostics, contract_path=diagnostics_package())
    document = readiness(settings)
    assert document["status"] == "not_ready"
    assert document["checks"]["database"] == "not_configured"
    assert not (tmp_path / "never.sqlite").exists()
    assert not (tmp_path / "never.sqlite-wal").exists()


def test_the_probe_never_switches_the_journal_mode_or_leaves_a_sidecar(tmp_path, diagnostics):
    """No `Store`, no `BEGIN IMMEDIATE`, no WAL switch: a probe reads, so the mode is unchanged.

    The database here is built in SQLite's own rollback mode, which is the mode a probe reading with
    `mode=ro` cannot change without taking a write lock. `Store` would have switched it to WAL on
    its very first transaction, so an unchanged mode is direct evidence that no `Store` was built
    and no write transaction was opened.
    """
    database = tmp_path / "plain.sqlite"
    connection = sqlite3.connect(str(database))
    try:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "delete"
        connection.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute("INSERT INTO metadata VALUES ('schema', '3')")
        connection.execute("INSERT INTO metadata VALUES ('source_instance', 'synthetic')")
        connection.execute("INSERT INTO metadata VALUES ('source_revision', '1')")
        connection.execute("INSERT INTO metadata VALUES ('source_recovery', 'clean')")
        connection.commit()
    finally:
        connection.close()
    config = {"mode": "source_sync", "database_path": str(database)}
    path = tmp_path / "chat.json"
    path.write_text(canonical(config), encoding="utf-8")
    subject = SimpleNamespace(path=path, config=config, tmp_path=tmp_path)
    settings = settings_for(CHAT_SERVICE, subject, diagnostics, contract_path=diagnostics_package())
    assert readiness(settings)["checks"]["database"] == "ok"
    connection = sqlite3.connect(str(database))
    try:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "delete"
    finally:
        connection.close()
    assert not (tmp_path / "plain.sqlite-wal").exists()
    assert not (tmp_path / "plain.sqlite-shm").exists()


def test_an_older_schema_is_not_ready_and_is_never_migrated(tmp_path, contracts, diagnostics):
    store = Store(tmp_path / "old.sqlite")
    store.migrate_profiles(tmp_path / "before-profiles.sqlite")
    config = {
        "mode": "source_sync",
        "database_path": str(store.path),
        "source_sync": {"recovery_path": str(store.recovery_path)},
    }
    path = tmp_path / "chat.json"
    path.write_text(canonical(config), encoding="utf-8")
    subject = SimpleNamespace(path=path, config=config, tmp_path=tmp_path)
    before = Path(store.path).read_bytes()
    settings = settings_for(CHAT_SERVICE, subject, diagnostics, contract_path=diagnostics_package())
    document = readiness(settings)
    assert document["checks"]["database"] == "failed"
    assert document["status"] == "not_ready"
    # An older schema needs an operator, not a probe: the bytes are exactly what they were.
    assert Path(store.path).read_bytes() == before


def test_a_missing_guard_is_not_ready_and_is_never_reinitialized(chat_service, diagnostics):
    guard = chat_service.store.recovery_path
    guard.unlink()
    settings = settings_for(
        CHAT_SERVICE, chat_service, diagnostics, contract_path=diagnostics_package()
    )
    document = readiness(settings)
    assert document["checks"]["guard"] == "not_configured"
    assert document["status"] == "not_ready"
    assert not guard.exists(), "the probe must never re-create a checkpoint"


def test_a_divergent_guard_is_not_ready_and_is_never_repaired(chat_service, diagnostics):
    guard = chat_service.store.recovery_path
    original = json.loads(guard.read_text(encoding="utf-8"))
    tampered = dict(original, revision=original["revision"] + 1)
    guard.write_text(canonical(tampered), encoding="utf-8")
    settings = settings_for(
        CHAT_SERVICE, chat_service, diagnostics, contract_path=diagnostics_package()
    )
    assert readiness(settings)["checks"]["guard"] == "failed"
    assert json.loads(guard.read_text(encoding="utf-8")) == tampered
    assert probe_guard(chat_service.store.path, guard).state == "failed"


def test_a_guard_that_disagrees_is_never_silently_rebuilt(chat_service, diagnostics):
    guard = chat_service.store.recovery_path
    guard.write_text("{not json", encoding="utf-8")
    settings = settings_for(
        CHAT_SERVICE, chat_service, diagnostics, contract_path=diagnostics_package()
    )
    assert readiness(settings)["checks"]["guard"] == "failed"
    assert guard.read_text(encoding="utf-8") == "{not json"


def test_a_fixture_backed_process_is_never_ready(chat_service, diagnostics):
    """The production container refuses `local_fixture`: synthetic sources are not a deployment."""
    assert probe_mode({"mode": "local_fixture"}).state == "failed"
    assert probe_mode({}).state == "failed"
    assert probe_mode({"mode": "something_else"}).state == "failed"
    assert probe_mode({"mode": "source_sync"}).state == "ok"


def test_an_unconfigured_log_is_non_durable_and_never_ready(chat_service, tmp_path):
    temporary = Diagnostics(CHAT_SERVICE, {})
    settings = settings_for(
        CHAT_SERVICE, chat_service, temporary, contract_path=diagnostics_package()
    )
    document = readiness(settings)
    assert document["checks"]["log"] == "non_durable"
    assert document["status"] == "not_ready"
    assert NON_DURABLE.state == "non_durable"


def test_a_log_latch_makes_a_process_not_ready(tmp_path, chat_service):
    directory = tmp_path / "logs"
    directory.mkdir()
    adapter = Diagnostics(CHAT_SERVICE, {"log_directory": str(directory)})
    adapter.sink.directory_bytes = 1
    assert adapter.emit("runtime.starting", level="INFO", outcome="started") is False
    settings = settings_for(
        CHAT_SERVICE, chat_service, adapter, contract_path=diagnostics_package()
    )
    assert probe_log(adapter).state == "failed"
    assert readiness(settings)["checks"]["log"] == "failed"
    assert readiness(settings)["status"] == "not_ready"


def test_the_log_probe_creates_nothing_and_writes_no_event(chat_service, tmp_path):
    directory = tmp_path / "logs"
    directory.mkdir()
    adapter = Diagnostics(CHAT_SERVICE, {"log_directory": str(directory)})
    before = directory_listing(directory)
    settings = settings_for(
        CHAT_SERVICE, chat_service, adapter, contract_path=diagnostics_package()
    )
    for _ in range(3):
        readiness(settings)
    # Not one event line, and not one file: a probe records nothing, and the sequence never moves.
    assert directory_listing(directory) == before
    assert adapter.sink.sequence == 0


def test_a_drifted_contract_package_is_not_ready(tmp_path):
    directory = tmp_path / "v1"
    directory.mkdir()
    (directory / "manifest.json").write_text(
        json.dumps({"version": "1.0.0", "files": {"event.schema.json": "0" * 64}}),
        encoding="utf-8",
    )
    (directory / "event.schema.json").write_text("{}", encoding="utf-8")
    assert probe_contract(directory).state == "failed"
    assert probe_contract(None).state == NOT_CONFIGURED.state
    assert probe_contract(tmp_path / "absent").state == "not_configured"


def test_a_contract_package_from_another_version_is_not_ready(tmp_path):
    directory = tmp_path / "v1"
    directory.mkdir()
    body = b"{}"
    (directory / "event.schema.json").write_bytes(body)
    (directory / "manifest.json").write_text(
        json.dumps(
            {
                "version": "2.0.0",
                "files": {"event.schema.json": hashlib.sha256(body).hexdigest()},
            }
        ),
        encoding="utf-8",
    )
    assert probe_contract(directory).state == "failed"


def test_the_published_package_verifies_exactly_as_the_deployment_kept_it():
    assert probe_contract(diagnostics_package()).state == "ok"


def test_an_unregistered_client_is_not_ready(knowledge_service, diagnostics, tmp_path):
    knowledge = dict(knowledge_service.config)
    knowledge["knowledge"] = dict(knowledge["knowledge"], clients={})
    path = tmp_path / "knowledge.json"
    path.write_text(canonical(knowledge), encoding="utf-8")
    subject = SimpleNamespace(path=path, config=knowledge, tmp_path=tmp_path)
    settings = settings_for(
        KNOWLEDGE_SERVICE,
        subject,
        diagnostics,
        contract_path=diagnostics_package(),
        client=CLIENT,
    )
    assert readiness(settings)["checks"]["client"] == "failed"


def test_a_client_without_a_permission_is_not_ready(knowledge_service, diagnostics, tmp_path):
    knowledge = dict(knowledge_service.config)
    clients = dict(knowledge["knowledge"]["clients"])
    clients[CLIENT] = dict(clients[CLIENT], permissions=[])
    knowledge["knowledge"] = dict(knowledge["knowledge"], clients=clients)
    path = tmp_path / "knowledge.json"
    path.write_text(canonical(knowledge), encoding="utf-8")
    subject = SimpleNamespace(path=path, config=knowledge, tmp_path=tmp_path)
    settings = settings_for(
        KNOWLEDGE_SERVICE,
        subject,
        diagnostics,
        contract_path=diagnostics_package(),
        client=CLIENT,
    )
    assert readiness(settings)["checks"]["client"] == "failed"


def test_a_missing_configuration_is_not_configured_not_a_crash(tmp_path, diagnostics):
    subject = SimpleNamespace(path=tmp_path / "absent.json", tmp_path=tmp_path)
    settings = settings_for(CHAT_SERVICE, subject, diagnostics, contract_path=None)
    assert probe_configuration(settings).state == NOT_CONFIGURED.state
    document = readiness(settings)
    assert document["status"] == "not_ready"
    assert set(document["checks"]) == set(CHECKS_BY_SERVICE[CHAT_SERVICE])
    # Every check that depends on the configuration is a verdict, not an exception: the process is
    # running and assembled, so those two are honestly ok while nothing file-backed can be.
    assert document["checks"]["configuration"] == "not_configured"
    assert document["checks"]["contract"] == "not_configured"
    assert document["checks"]["database"] in {"not_configured", "failed"}
    assert document["checks"]["assembled"] == "ok"


def test_an_unparsable_configuration_fails_rather_than_reading_as_absent(tmp_path, diagnostics):
    path = tmp_path / "chat.json"
    path.write_text("{not json", encoding="utf-8")
    subject = SimpleNamespace(path=path, tmp_path=tmp_path)
    settings = settings_for(CHAT_SERVICE, subject, diagnostics, contract_path=None)
    assert probe_configuration(settings).state == "failed"


def test_a_configuration_without_a_database_path_fails(tmp_path, diagnostics):
    path = tmp_path / "chat.json"
    path.write_text(canonical({"mode": "source_sync"}), encoding="utf-8")
    subject = SimpleNamespace(path=path, tmp_path=tmp_path)
    settings = settings_for(CHAT_SERVICE, subject, diagnostics, contract_path=None)
    assert probe_configuration(settings).state == "failed"
    assert probe_database("").state in {"not_configured", "failed"}


def test_a_released_owner_makes_a_process_not_ready(chat_service, diagnostics):
    """The one condition no file can express: the object that owns the handle is gone."""
    settings = settings_for(
        CHAT_SERVICE, chat_service, diagnostics, contract_path=diagnostics_package()
    )
    settings.runtime = SimpleNamespace(active=True, released=True)
    document = readiness(settings)
    assert document["checks"]["owner"] == "failed"
    assert document["status"] == "not_ready"


def test_a_shutting_down_runtime_never_reports_ready(chat_service, diagnostics):
    settings = settings_for(
        CHAT_SERVICE, chat_service, diagnostics, contract_path=diagnostics_package()
    )
    settings.runtime = SimpleNamespace(active=False, released=False)
    document = readiness(settings)
    assert document["status"] == "not_ready"
    assert set(document["checks"]) == set(CHECKS_BY_SERVICE[CHAT_SERVICE])


def test_an_entry_whose_handle_is_unconfirmed_is_not_ready(chat_service, diagnostics):
    settings = settings_for(
        CHAT_SERVICE,
        chat_service,
        diagnostics,
        contract_path=diagnostics_package(),
        handles=lambda: False,
    )
    assert readiness(settings)["checks"]["owner"] == "failed"
    settings.handles = lambda: True
    assert readiness(settings)["checks"]["owner"] == "ok"


def test_a_handle_callable_that_raises_is_a_verdict_not_an_operator_error(
    chat_service, diagnostics
):
    def broken():
        raise RuntimeError("synthetic defect")

    settings = settings_for(
        CHAT_SERVICE,
        chat_service,
        diagnostics,
        contract_path=diagnostics_package(),
        handles=broken,
    )
    assert readiness(settings)["checks"]["owner"] == "failed"


def test_an_unassembled_process_cannot_claim_to_be_assembled(chat_service, diagnostics):
    settings = settings_for(
        CHAT_SERVICE, chat_service, diagnostics, contract_path=diagnostics_package()
    )
    assert assess(settings, diagnostics, None)["assembled"].state == NOT_CONFIGURED.state
    settings.runtime = None
    # Nothing to report about a runtime that was never assembled, and that alone keeps this process
    # out of the ready verdict: the contract requires the runtime to be really assembled.
    assert readiness(settings)["checks"]["assembled"] == "not_configured"
    assert readiness(settings)["status"] == "not_ready"


def test_probe_ownership_without_a_runtime_is_not_configured():
    assert probe_ownership(None, None).state == NOT_CONFIGURED.state
    assert probe_ownership(SimpleNamespace(released=False), None).state == OK.state


def test_a_base_only_installation_never_claims_the_extensions_it_lacks(tmp_path, contracts):
    """A schema family that was never installed must not be reported as ready.

    One reviewed migration installs the knowledge schema and every option family together, so the
    state under test is built by removing a marker: this is the database of a deployment whose
    catalogue step has not run. The probe must report the families really held and must not claim
    the one that is absent.
    """
    store = Store(tmp_path / "base.sqlite")
    store.migrate_profiles(tmp_path / "before-profiles.sqlite")
    store.migrate_sources(tmp_path / "before-sources.sqlite", contracts)
    migrate_knowledge(store, tmp_path / "before-knowledge.sqlite")
    with store.transaction() as db:
        db.execute("DELETE FROM metadata WHERE key='knowledge_catalog_schema'")
    assert probe_installed(store.path, "knowledge_schema", "1").state == OK.state
    assert probe_installed(store.path, "knowledge_catalog_schema").state == "failed"
    # The catalogue this build serves is absent: the process reports that instead of claiming it.
    assert probe_knowledge_extensions(store.path).state == NOT_CONFIGURED.state


def test_a_partial_optional_installation_is_never_called_consistent(tmp_path, contracts):
    """One optional family without the others is not a state any migration sequence produces."""
    store = Store(tmp_path / "partial.sqlite")
    store.migrate_profiles(tmp_path / "before-profiles.sqlite")
    store.migrate_sources(tmp_path / "before-sources.sqlite", contracts)
    migrate_knowledge(store, tmp_path / "before-knowledge.sqlite")
    with store.transaction() as db:
        db.execute("DELETE FROM metadata WHERE key='research_notes_schema'")
    assert probe_installed(store.path, "lessons_schema").state == "ok"
    assert probe_installed(store.path, "research_notes_schema").state == "failed"
    assert probe_knowledge_extensions(store.path).state == "failed"


def test_a_database_without_the_base_family_is_a_failure_not_an_absent_capability(
    chat_service,
):
    """A chat database is not a project-knowledge database, and must not be read as one."""
    assert probe_installed(chat_service.store.path, "knowledge_schema").state == "failed"
    assert probe_knowledge_extensions(chat_service.store.path).state == "failed"


def test_an_installed_marker_is_read_not_written(chat_service):
    assert probe_installed(chat_service.store.path, "schema", "3").state == OK.state
    assert probe_installed(chat_service.store.path, "schema", "2").state == "failed"
    assert probe_installed(chat_service.store.path, "absent_key").state == "failed"


def test_only_the_registered_service_keys_are_ever_reported(chat_service, diagnostics):
    for service, expected in CHECKS_BY_SERVICE.items():
        assert keys_for(service) == expected
    settings = settings_for(
        CHAT_SERVICE, chat_service, diagnostics, contract_path=diagnostics_package()
    )
    assert set(readiness(settings)["checks"]) == set(CHECKS_BY_SERVICE[CHAT_SERVICE])


def test_the_readiness_token_is_independent_of_every_business_permission(monkeypatch):
    adapter = Diagnostics(CHAT_SERVICE, {"diagnostics": {"token_env": "TS102_READINESS_TOKEN"}})
    monkeypatch.delenv("TS102_READINESS_TOKEN", raising=False)
    assert present_token(request_with(None), adapter) == "unconfigured"
    monkeypatch.setenv("TS102_READINESS_TOKEN", "synthetic-readiness-token")
    assert present_token(request_with(None), adapter) == "unauthorized"
    assert present_token(request_with("Bearer wrong"), adapter) == "unauthorized"
    assert present_token(request_with("Basic synthetic-readiness-token"), adapter) == "unauthorized"
    assert present_token(request_with("Bearer "), adapter) == "unauthorized"
    assert present_token(request_with("Bearer synthetic-readiness-token"), adapter) == "ok"


def test_a_process_with_no_token_name_at_all_cannot_authenticate_anyone(monkeypatch):
    adapter = Diagnostics(CHAT_SERVICE, {})
    monkeypatch.setenv("TS102_READINESS_TOKEN", "synthetic-readiness-token")
    assert (
        present_token(request_with("Bearer synthetic-readiness-token"), adapter) == "unconfigured"
    )


def request_with(authorization):
    headers = [] if authorization is None else [(b"authorization", authorization.encode())]
    return Request({"type": "http", "method": "GET", "path": "/health/ready", "headers": headers})


def test_the_configuration_read_is_the_readiness_check_that_matters(chat_service, diagnostics):
    """A process that can name its own prerequisites is the minimum; the rest is verified."""
    settings = settings_for(
        CHAT_SERVICE, chat_service, diagnostics, contract_path=diagnostics_package()
    )
    assert probe_configuration(settings).state == OK.state
    assert source_recovery.checkpoint(sqlite3.connect(str(chat_service.store.path))) is not None
