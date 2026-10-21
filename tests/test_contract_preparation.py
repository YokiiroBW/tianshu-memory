"""Reuse immutable rule preparation, never a validation result or input document."""

import copy
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from jsonschema import Draft202012Validator, FormatChecker, ValidationError
from referencing import Registry, Resource

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
        "$defs": {"address": {"type": "string", "format": "ipv4"}},
    }
    loaded.schemas["synthetic"] = schema
    loaded.registry = loaded.registry.with_resource(schema["$id"], Resource.from_contents(schema))
    loaded.validate("synthetic#address", "127.0.0.1")
    with pytest.raises(ValidationError) as bad_format:
        loaded.validate("synthetic#address", "999.0.0.1")
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

    def prepare(*args, **kwargs):
        validator = Draft202012Validator(*args, **kwargs)
        prepared.append(validator)
        return validator

    monkeypatch.setattr(contract_module, "Draft202012Validator", prepare)
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
