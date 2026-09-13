"""Validate the small candidate against the immutable local v1 dependency; no remote refs."""

import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError
from referencing import Resource

from tianshu_memory.contracts import Contracts


def validate(directory, dependency):
    directory = Path(directory)
    schema = json.loads((directory / "schemas/profiles.json").read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    registry = dependency.registry.with_resource(schema["$id"], Resource.from_contents(schema))
    cases = json.loads((directory / "examples.json").read_text(encoding="utf-8"))
    for case in cases:
        validator = Draft202012Validator(
            {"$ref": schema["$id"] + "#/$defs/" + case["definition"]},
            registry=registry,
            format_checker=FormatChecker(),
        )
        try:
            validator.validate(case["document"])
            valid = True
        except ValidationError:
            valid = False
        assert valid == case["valid"], case["id"]
    return len(cases)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--dependency", required=True)
    args = parser.parse_args()
    print(f"{validate(Path(__file__).parent, Contracts(args.dependency))} candidate cases passed")
