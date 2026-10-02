import hashlib
import importlib.util
import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

MANIFEST_SHA256 = "81e6cc4ddef7c6f82e055d4cb04b090db036dd5c52763473ce697aa02db478a1"
PROFILE_MANIFEST_SHA256 = "488d05438dd5b5abaa43a66a7eab0eb5cf615d5af01a964a7286cd23e68f7eb7"
SOURCE_MANIFEST_SHA256 = "178d0ce66210bdfad4cfb85d8b5f0905b0b67f834e2a530efe5636ff0373633d"
SOURCE_BATCH_MANIFEST_SHA256 = "82e8d9043eb2f9a87f0efcb56a16e8f8bb6b119c6cf1683af48e3049024b380b"


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
        schemas = {}
        for relative, expected in manifest["sha256"].items():
            path = self.directory.parent.parent / relative
            if not path.resolve().is_relative_to(self.directory):
                raise ValueError("Contract path escapes version directory")
            contents = path.read_bytes()
            if digest(contents) != expected:
                raise ValueError(f"Contract file hash mismatch: {relative}")
            if path.parent == self.directory / "schemas" and path.suffix == ".json":
                schemas[path.stem] = json.loads(contents)
        self.schemas = schemas
        self.registry = Registry().with_resources(
            (s["$id"], Resource.from_contents(s)) for s in self.schemas.values()
        )
        self.profile_version = None
        self.source_version = None
        self.source_rules = None
        self.source_batch_version = None
        self.source_batch_rules = None

    def load_profiles(self):
        directory = self.directory.parent.parent / "profile-memory/v1"
        manifest_bytes = (directory / "manifest.json").read_bytes()
        if digest(manifest_bytes) != PROFILE_MANIFEST_SHA256:
            raise ValueError("Profile contract manifest hash mismatch")
        manifest = json.loads(manifest_bytes)
        if (
            manifest["version"] != "1.0.0"
            or manifest["version_domain"] != "profile-memory/v1"
            or manifest["dependency"]["manifest_sha256"] != MANIFEST_SHA256
        ):
            raise ValueError("Unsupported profile contract")
        for relative, expected in manifest["sha256"].items():
            path = directory / relative
            if not path.resolve().is_relative_to(directory.resolve()):
                raise ValueError("Profile contract path escapes version directory")
            if digest(path.read_bytes()) != expected:
                raise ValueError(f"Profile contract file hash mismatch: {relative}")
        schema = json.loads((directory / "schemas/profiles.json").read_text(encoding="utf-8"))
        self.registry = self.registry.with_resource(schema["$id"], Resource.from_contents(schema))
        self.schemas["profiles"] = schema
        self.profile_version = manifest["version"]

    def load_sources(self):
        """Register the published source package and freshly verify both dependencies.

        Its pure relationship assertions supply no authentication or storage authority.
        Load verified source bytes directly so no cache is written to the published package.
        """
        directory = self.directory.parent.parent / "source-sync/v1"
        manifest_bytes = (directory / "manifest.json").read_bytes()
        if digest(manifest_bytes) != SOURCE_MANIFEST_SHA256:
            raise ValueError("Source contract manifest hash mismatch")
        manifest = json.loads(manifest_bytes)
        if (
            manifest["package"] != "source-sync/v1"
            or manifest["version"] != "1.0.0"
            or manifest["schema_version"] != 1
            or manifest["dependencies"]
            != [
                {
                    "package": "text-dialogue/v1",
                    "version": "1.0.0",
                    "manifest_sha256": MANIFEST_SHA256,
                },
                {
                    "package": "profile-memory/v1",
                    "version": "1.0.0",
                    "manifest_sha256": PROFILE_MANIFEST_SHA256,
                },
            ]
        ):
            raise ValueError("Unsupported source contract")
        files = {}
        for relative, expected in manifest["sha256"].items():
            path = directory / relative
            if not path.resolve().is_relative_to(directory.resolve()):
                raise ValueError("Source contract path escapes version directory")
            contents = path.read_bytes()
            if digest(contents) != expected:
                raise ValueError(f"Source contract file hash mismatch: {relative}")
            files[relative] = contents
        verified = Contracts(self.directory)
        verified.load_profiles()
        schemas = {
            "sync-" + name: json.loads(files[f"schemas/{name}.json"])
            for name in ("shared", "sources", "workflow")
        }
        if [schema["$id"] for schema in schemas.values()] != manifest["schema_ids"]:
            raise ValueError("Source contract schema identifiers mismatch")
        registry = verified.registry.with_resources(
            (schema["$id"], Resource.from_contents(schema)) for schema in schemas.values()
        )
        rules_path = directory / "rules.py"
        spec = importlib.util.spec_from_file_location("tianshu_source_sync_v1_rules", rules_path)
        rules = importlib.util.module_from_spec(spec)
        exec(compile(files["rules.py"], str(rules_path), "exec"), rules.__dict__)
        self.registry = registry
        self.schemas = verified.schemas | schemas
        self.profile_version = verified.profile_version
        self.source_rules = rules
        self.source_version = manifest["version"]
        self.source_batch_version = None
        self.source_batch_rules = None

    def load_source_batches(self):
        """Bind the compatible batching semantics without changing owner wire contracts."""
        directory = self.directory.parent.parent / "source-sync-batch/v1"
        manifest_bytes = (directory / "manifest.json").read_bytes()
        if digest(manifest_bytes) != SOURCE_BATCH_MANIFEST_SHA256:
            raise ValueError("Source batch contract manifest hash mismatch")
        manifest = json.loads(manifest_bytes)
        if (
            manifest["package"] != "source-sync-batch/v1"
            or manifest["version"] != "1.0.0"
            or manifest["schema_version"] != 1
            or manifest["dependencies"]
            != [
                {
                    "package": "source-sync/v1",
                    "version": "1.0.0",
                    "manifest_sha256": SOURCE_MANIFEST_SHA256,
                }
            ]
        ):
            raise ValueError("Unsupported source batch contract")
        files = {}
        for relative, expected in manifest["sha256"].items():
            path = directory / relative
            if not path.resolve().is_relative_to(directory.resolve()):
                raise ValueError("Source batch contract path escapes version directory")
            contents = path.read_bytes()
            if digest(contents) != expected:
                raise ValueError(f"Source batch contract file hash mismatch: {relative}")
            files[relative] = contents
        verified = Contracts(self.directory)
        verified.load_sources()
        schema = json.loads(files["schemas/batch.json"])
        if schema["$id"] != "https://contracts.tianshu.invalid/source-sync-batch/v1/batch.json":
            raise ValueError("Source batch schema identifier mismatch")
        rules_path = directory / "rules.py"
        spec = importlib.util.spec_from_file_location(
            "tianshu_source_sync_batch_v1_rules", rules_path
        )
        rules = importlib.util.module_from_spec(spec)
        exec(compile(files["rules.py"], str(rules_path), "exec"), rules.__dict__)
        self.registry = verified.registry.with_resource(
            schema["$id"], Resource.from_contents(schema)
        )
        self.schemas = verified.schemas | {"sync-batch": schema}
        self.source_rules, self.source_version = verified.source_rules, verified.source_version
        self.profile_version = verified.profile_version
        self.source_batch_rules, self.source_batch_version = rules, manifest["version"]

    def validate(self, name: str, document: dict):
        module, definition = name.split("#")
        Draft202012Validator(
            {"$ref": f"{self.schemas[module]['$id']}#/$defs/{definition}"},
            registry=self.registry,
            format_checker=FormatChecker(),
        ).validate(document)
