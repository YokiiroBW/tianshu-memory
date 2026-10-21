"""Reuse immutable rule preparation, never a validation result or input document."""

import copy
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from jsonschema import Draft202012Validator, FormatChecker, ValidationError
from jsonschema.validators import validator_for
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from tianshu_memory import contracts as contract_module
from tianshu_memory.contracts import Contracts


def fresh_validation(contracts, name, document):
    module, definition = name.split("#")
    Draft202012Validator(
        {"$ref": f"{contracts.schemas[module]['$id']}#/$defs/{definition}"},
        registry=contracts.registry,
        format_checker=FormatChecker(),
    ).validate(document)


def error_details(error):
    return (
        error.message,
        error.validator,
        tuple(error.path),
        tuple(error.schema_path),
        [error_details(child) for child in error.context],
    )


def outcome(validate, *args):
    try:
        validate(*args)
    except ValidationError as error:
        return error_details(error)
    return None


def test_prepared_rules_match_fresh_validator_on_published_documents_and_bad_inputs(contracts):
    loaded = Contracts(contracts.directory)
    loaded.load_sources()
    for package, prefix in [("text-dialogue", ""), ("source-sync", "sync-")]:
        documents = json.loads(
            (loaded.directory.parent.parent / package / "v1/examples/documents.json").read_bytes()
        )
        for example in documents:
            name, document = example["schema"], example["document"]
            if prefix:
                if name.startswith("text-"):
                    name = name.removeprefix("text-")
                else:
                    name = prefix + name
            # Nulls, wrong types, missing fields and extra fields also retain the
            # first error, nested contexts and reference/format error paths.
            variants = [document, None, [], "wrong-type", {}, dict(document, unexpected=True)]
            for variant in variants:
                assert outcome(loaded.validate, name, variant) == outcome(
                    fresh_validation, loaded, name, variant
                ), example["id"]


def test_prepared_validator_rechecks_mutated_input_and_all_formats(contracts):
    loaded = Contracts(contracts.directory)
    document = dict(
        schema_version=1,
        request_id="synthetic-request",
        origin={"assertion_ref": "synthetic-origin"},
        idempotency_key="synthetic-key",
        deadline_at="2030-01-01T00:00:00Z",
    )
    loaded.validate("common#command", document)
    document["deadline_at"] = "2030-99-01T00:00:00Z"
    # jsonschema's optional date-time checker depends on the installed extras.
    # Preserve exactly the fresh checker's behavior in this environment.
    assert outcome(loaded.validate, "common#command", document) == outcome(
        fresh_validation, loaded, "common#command", document
    )
    document["deadline_at"] = "2030-01-01T00:00:00Z"
    document.pop("origin")
    with pytest.raises(ValidationError) as missing:
        loaded.validate("common#command", document)
    assert missing.value.validator == "required"
    document["origin"] = {"assertion_ref": "changed-current-origin"}
    loaded.validate("common#command", document)

    schema = {
        "$id": "https://synthetic.tianshu.invalid/format.json",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$defs": {
            "address": {"type": "string", "format": "ipv4"},
            "use": {"$ref": "#/$defs/address"},
        },
    }
    loaded.schemas["synthetic"] = schema
    loaded.registry = loaded.registry.with_resource(schema["$id"], Resource.from_contents(schema))
    loaded.validate("synthetic#use", "127.0.0.1")
    with pytest.raises(ValidationError) as bad_format:
        loaded.validate("synthetic#use", "999.0.0.1")
    assert bad_format.value.validator == "format"


def test_registry_resource_replacement_invalidates_prepared_rules(contracts):
    loaded = Contracts(contracts.directory)
    identity = "https://synthetic.tianshu.invalid/validation.json"

    def replace(maximum):
        schema = {
            "$id": identity,
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$defs": {"value": {"type": "integer", "maximum": maximum}},
        }
        loaded.schemas["synthetic"] = schema
        loaded.registry = loaded.registry.with_resource(identity, Resource.from_contents(schema))

    replace(10)
    loaded.validate("synthetic#value", 7)
    replace(5)
    with pytest.raises(ValidationError, match="greater than the maximum"):
        loaded.validate("synthetic#value", 7)
    replace(10)
    loaded.validate("synthetic#value", 7)


def test_registry_replaced_during_crawl_is_not_mislabeled_as_prepared(contracts, monkeypatch):
    loaded = Contracts(contracts.directory)
    schema = {
        "$id": "https://synthetic.tianshu.invalid/race.json",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$defs": {"value": {"type": "integer", "maximum": 10}},
    }
    loaded.schemas["synthetic"] = schema
    original = loaded.registry.with_resource(schema["$id"], Resource.from_contents(schema))
    replacement = original.with_resource(
        schema["$id"],
        Resource.from_contents(dict(schema, **{"$defs": {"value": {"maximum": 5}}})),
    )
    loaded.registry = original
    crawl = Registry.crawl

    def replace_during_crawl(registry):
        if registry is original:
            loaded.registry = replacement
        return crawl(registry)

    monkeypatch.setattr(Registry, "crawl", replace_during_crawl)
    loaded.validate("synthetic#value", 7)
    with pytest.raises(ValidationError, match="greater than the maximum"):
        loaded.validate("synthetic#value", 7)


def test_load_invalidates_preparation_and_concurrent_calls_validate_each_document(
    contracts, monkeypatch
):
    loaded = Contracts(contracts.directory)
    prepared = []

    prepare_validation = contract_module._prepare_validation

    def prepare_registry(registry):
        registry, validator_type = prepare_validation(registry)

        def prepare(*args, **kwargs):
            validator = validator_type(*args, **kwargs)
            prepared.append(validator)
            return validator

        return registry, prepare

    monkeypatch.setattr(contract_module, "_prepare_validation", prepare_registry)
    scope = dict(
        actor_id="synthetic-actor",
        person_id="synthetic-person",
        audience="self_private",
        conversation_id="synthetic-conversation",
    )
    loaded.validate("common#scope", scope)
    loaded.validate("common#scope", scope)
    assert len(prepared) == 1
    loaded.load_sources()

    def validate(index):
        current = copy.deepcopy(scope)
        if index % 2:
            current["person_id"] = None
        return outcome(loaded.validate, "common#scope", current)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(validate, range(48)))
    assert [error is not None for error in results] == [bool(i % 2) for i in range(48)]
    assert len(prepared) == 2 and prepared[0] is not prepared[1]


def test_missing_reference_still_fails_after_preparation(contracts):
    loaded = Contracts(contracts.directory)
    # The published registry never gains a network retriever. Repeated calls
    # still fail to resolve the missing definition rather than becoming valid.
    for _ in range(2):
        with pytest.raises(Exception) as prepared:
            loaded.validate("common#synthetic-missing-definition", {})
        with pytest.raises(type(prepared.value)) as fresh:
            fresh_validation(loaded, "common#synthetic-missing-definition", {})
        assert str(prepared.value) == str(fresh.value)


def local_contracts(contracts, schemas, *, registry=None):
    loaded = Contracts(contracts.directory)
    loaded.schemas = schemas
    loaded.registry = (
        registry
        if registry is not None
        else Registry().with_resources(
            (schema["$id"], Resource.from_contents(schema)) for schema in schemas.values()
        )
    )
    return loaded


def lookup_preparations(monkeypatch):
    calls = []
    resolver = Registry.resolver

    def observed(registry, base_uri=""):
        calls.append(base_uri)
        return resolver(registry, base_uri=base_uri)

    monkeypatch.setattr(Registry, "resolver", observed)
    return calls


def test_static_relative_nested_ids_anchors_and_siblings_keep_current_error_paths(
    contracts, monkeypatch
):
    root = {
        "$schema": Draft202012Validator.META_SCHEMA["$id"],
        "$id": "https://synthetic.tianshu.invalid/rules/root.json",
        "$defs": {
            "container": {
                "$id": "nested/",
                "$defs": {
                    "value": {
                        "$id": "leaf.json",
                        "$anchor": "value",
                        "type": "integer",
                        "minimum": 2,
                    },
                    "use": {"$ref": "leaf.json#value"},
                },
                "type": "object",
                "properties": {
                    "a": {"$ref": "leaf.json#value"},
                    "b": {"$ref": "#/$defs/use", "maximum": 5},
                },
                "required": ["a", "b"],
                "additionalProperties": False,
            }
        },
    }
    loaded = local_contracts(contracts, {"local": root})
    preparations = lookup_preparations(monkeypatch)
    variants = [
        {"a": 2, "b": 3},
        {"a": 3, "b": 5},
        {"a": 1, "b": 9},
        {"a": "bad", "b": 2},
        {"a": 2},
    ]
    for document in variants:
        assert outcome(loaded.validate, "local#container", document) == outcome(
            fresh_validation, loaded, "local#container", document
        )
    assert "https://synthetic.tianshu.invalid/rules/nested/" in preparations
    before = list(preparations)
    loaded.validate("local#container", {"a": 4, "b": 4})
    assert preparations == before
    assert validator_for({"$schema": root["$schema"]}) is Draft202012Validator


def test_ambiguous_shared_schema_node_uses_each_official_resolution_base(contracts, monkeypatch):
    draft = Draft202012Validator.META_SCHEMA["$id"]
    shared = {"$ref": "child.json#/$defs/value"}
    schemas = {}
    for name, kind in [("a", "integer"), ("b", "string")]:
        prefix = f"https://synthetic.tianshu.invalid/{name}/"
        schemas[name] = {"$schema": draft, "$id": prefix + "root.json", "$defs": {"value": shared}}
        schemas[name + "-child"] = {
            "$schema": draft,
            "$id": prefix + "child.json",
            "$defs": {"value": {"type": kind}},
        }
    loaded = local_contracts(contracts, schemas)
    preparations = lookup_preparations(monkeypatch)
    for name, document in [("a", 3), ("b", "valid"), ("a", "invalid"), ("b", 3)]:
        assert outcome(loaded.validate, name + "#value", document) == outcome(
            fresh_validation, loaded, name + "#value", document
        )
    assert preparations == []


@pytest.mark.parametrize(
    "keyword", ["$dynamicRef", "$dynamicAnchor", "$recursiveRef", "$recursiveAnchor"]
)
def test_any_dynamic_keyword_keeps_the_entire_graph_on_official_validation(contracts, keyword):
    schema = {
        "$schema": Draft202012Validator.META_SCHEMA["$id"],
        "$id": "https://synthetic.tianshu.invalid/dynamic.json",
        "$defs": {
            "value": {"type": "integer"},
            "use": {"$ref": "#/$defs/value"},
            "unused": {
                keyword: True
                if keyword == "$recursiveAnchor"
                else "node"
                if keyword.endswith("Anchor")
                else "#node"
            },
        },
    }
    loaded = local_contracts(contracts, {"local": schema})
    _, validator = contract_module._prepare_validation(loaded.registry)
    assert validator is Draft202012Validator
    for document in [3, "bad"]:
        assert outcome(loaded.validate, "local#use", document) == outcome(
            fresh_validation, loaded, "local#use", document
        )


def test_dynamic_reference_keeps_fresh_official_scope_for_nested_instances(contracts):
    schema = {
        "$schema": Draft202012Validator.META_SCHEMA["$id"],
        "$id": "https://synthetic.tianshu.invalid/tree.json",
        "$defs": {
            "tree": {
                "$dynamicAnchor": "node",
                "type": "object",
                "properties": {"value": {"type": "integer"}, "next": {"$dynamicRef": "#node"}},
                "required": ["value"],
            }
        },
    }
    loaded = local_contracts(contracts, {"local": schema})
    for document in [{"value": 1, "next": {"value": 2}}, {"value": 1, "next": {"value": "bad"}}]:
        assert outcome(loaded.validate, "local#tree", document) == outcome(
            fresh_validation, loaded, "local#tree", document
        )


@pytest.mark.parametrize(
    "dialect",
    [
        None,
        "http://json-schema.org/draft-07/schema#",
        "https://synthetic.tianshu.invalid/unknown-dialect",
    ],
)
def test_non_draft202012_or_unknown_root_keeps_official_callback(contracts, dialect):
    schema = {
        "$id": "https://synthetic.tianshu.invalid/other.json",
        "$defs": {"value": {"type": "integer"}, "use": {"$ref": "#/$defs/value"}},
    }
    if dialect is not None:
        schema["$schema"] = dialect
    # Public resource construction allows an explicitly known specification for
    # this unknown-dialect oracle; the optimization must not infer it from a URI.
    registry = Registry().with_resource(schema["$id"], DRAFT202012.create_resource(schema))
    loaded = local_contracts(contracts, {"local": schema}, registry=registry)
    _, validator = contract_module._prepare_validation(registry)
    assert validator is Draft202012Validator
    for document in [3, "bad"]:
        assert outcome(loaded.validate, "local#use", document) == outcome(
            fresh_validation, loaded, "local#use", document
        )


def test_failed_local_reference_is_not_cached_and_preserves_official_exception(
    contracts, monkeypatch
):
    schema = {
        "$schema": Draft202012Validator.META_SCHEMA["$id"],
        "$id": "https://synthetic.tianshu.invalid/missing.json",
        "$defs": {"use": {"$ref": "#/$defs/missing"}},
    }
    loaded = local_contracts(contracts, {"local": schema})
    preparations = lookup_preparations(monkeypatch)
    for _ in range(3):
        with pytest.raises(Exception) as prepared:
            loaded.validate("local#use", {})
        with pytest.raises(type(prepared.value)) as fresh:
            fresh_validation(loaded, "local#use", {})
        assert str(prepared.value) == str(fresh.value)
    assert preparations == [schema["$id"]] * 3


def test_external_lookup_and_unknown_schema_context_are_not_static_cache_targets(
    contracts, monkeypatch
):
    draft = Draft202012Validator.META_SCHEMA["$id"]
    external = "https://synthetic.tianshu.invalid/external.json"
    schema = {
        "$schema": draft,
        "$id": "https://synthetic.tianshu.invalid/local.json",
        "$defs": {"value": {"type": "integer"}, "use": {"$ref": external + "#/$defs/value"}},
    }
    retrieved = []

    def retrieve(uri):
        retrieved.append(uri)
        assert uri == external
        return Resource.from_contents(
            {"$schema": draft, "$id": external, "$defs": {"value": {"type": "integer"}}}
        )

    registry = Registry(retrieve=retrieve).with_resource(
        schema["$id"], Resource.from_contents(schema)
    )
    loaded = local_contracts(contracts, {"local": schema}, registry=registry)
    preparations = lookup_preparations(monkeypatch)
    loaded.validate("local#use", 1)
    loaded.validate("local#use", 2)
    assert retrieved == [external, external] and preparations == []
    prepared_registry, validator_type = contract_module._prepare_validation(registry)
    foreign = validator_type({"$ref": schema["$id"] + "#/$defs/value"}, registry=prepared_registry)
    foreign.validate(3)
    with pytest.raises(ValidationError):
        foreign.validate("bad")
    assert preparations == []


def test_registry_replacement_discards_successful_static_resolved_targets(contracts):
    draft = Draft202012Validator.META_SCHEMA["$id"]
    child = "https://synthetic.tianshu.invalid/replace-child.json"
    schema = {
        "$schema": draft,
        "$id": "https://synthetic.tianshu.invalid/replace.json",
        "$defs": {"use": {"$ref": child + "#/$defs/value"}},
    }
    value = {"$schema": draft, "$id": child, "$defs": {"value": {"maximum": 10}}}
    loaded = local_contracts(contracts, {"local": schema, "child": value})
    loaded.validate("local#use", 7)
    loaded.registry = loaded.registry.with_resource(
        child, Resource.from_contents(dict(value, **{"$defs": {"value": {"maximum": 5}}}))
    )
    with pytest.raises(ValidationError):
        loaded.validate("local#use", 7)
