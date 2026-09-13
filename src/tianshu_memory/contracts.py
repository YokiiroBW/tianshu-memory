import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

MANIFEST_SHA256 = "81e6cc4ddef7c6f82e055d4cb04b090db036dd5c52763473ce697aa02db478a1"


def digest(data: bytes) -> str:
    return hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()


class Contracts:
    """Load only the immutable, locally published package; no network resolution."""

    def __init__(self, directory: str | Path):
        self.directory = Path(directory).resolve()
        manifest_bytes = (self.directory / "manifest.json").read_bytes()
        if digest(manifest_bytes) != MANIFEST_SHA256:
            raise ValueError("Contract manifest hash mismatch")
        manifest = json.loads(manifest_bytes)
        if manifest["version"] != "1.0.0":
            raise ValueError("Unsupported contract")
        for relative, expected in manifest["sha256"].items():
            path = self.directory.parent.parent / relative
            if not path.resolve().is_relative_to(self.directory):
                raise ValueError("Contract path escapes version directory")
            if digest(path.read_bytes()) != expected:
                raise ValueError(f"Contract file hash mismatch: {relative}")
        self.schemas = {
            p.stem: json.loads(p.read_text(encoding="utf-8"))
            for p in (self.directory / "schemas").glob("*.json")
        }
        self.registry = Registry().with_resources(
            (s["$id"], Resource.from_contents(s)) for s in self.schemas.values()
        )

    def validate(self, name: str, document: dict):
        module, definition = name.split("#")
        Draft202012Validator(
            {"$ref": f"{self.schemas[module]['$id']}#/$defs/{definition}"},
            registry=self.registry,
            format_checker=FormatChecker(),
        ).validate(document)
