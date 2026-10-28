"""Pinned isolated candidate; no remote JSON Schema resolution."""

import hashlib
import json
from datetime import datetime
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from ..domain import Fault

SCHEMA_SHA256 = "e7b5bccdddb160b3db241c7546c9721cfdde2c7e9e7fb545b3eceb675202195c"
MANIFEST_SHA256 = "9ce5c9ba6ae4545c843d1eac584da2ba10ca85e8c7fb1127d5adcf1b761e5c25"


class Contracts:
    def __init__(self, directory):
        directory = Path(directory)
        raw_manifest = (directory / "manifest.json").read_bytes().replace(b"\r\n", b"\n")
        if hashlib.sha256(raw_manifest).hexdigest() != MANIFEST_SHA256:
            raise ValueError("Actor manifest pin mismatch")
        manifest = json.loads(raw_manifest)
        if (
            manifest.get("name") != "actor-self-memory"
            or manifest.get("version") != "1.0.0-candidate.3"
        ):
            raise ValueError("Actor contract identity mismatch")
        for name, expected in manifest["sha256"].items():
            path = directory / name
            if (
                path.parent != directory
                or hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest() != expected
            ):
                raise ValueError("Actor contract hash mismatch")
        raw = (directory / "schema.json").read_bytes().replace(b"\r\n", b"\n")
        if hashlib.sha256(raw).hexdigest() != SCHEMA_SHA256:
            raise ValueError("Actor schema pin mismatch")
        self.schema = json.loads(raw)
        self.validators = {}

    def validate(self, definition, value):
        if definition not in self.validators:
            schema = {**self.schema, "$ref": "#/$defs/" + definition}
            self.validators[definition] = Draft202012Validator(
                schema, format_checker=FormatChecker()
            )
        self.validators[definition].validate(value)
        self._dates(value)

    def _dates(self, value):
        if isinstance(value, dict):
            for key, item in value.items():
                if (
                    key in {"occurred_at", "learned_at", "recorded_at", "checked_at"}
                    and item is not None
                ):
                    try:
                        parsed = datetime.fromisoformat(item.replace("Z", "+00:00"))
                        if parsed.tzinfo is None:
                            raise ValueError()
                    except (ValueError, TypeError, AttributeError):
                        raise Fault("invalid_input", 400) from None
                self._dates(item)
        elif isinstance(value, list):
            for item in value:
                self._dates(item)
