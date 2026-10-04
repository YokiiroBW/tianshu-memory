import hashlib
import importlib.util
import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

MANIFEST_SHA256 = "90697e6ecbb587d3db8c8e4682f7f8f43a8b1a98f70d8835c8282b03828d2d3a"
PROFILE_MANIFEST_SHA256 = "757b243a25945a75d55dafab9da94828e79a009a0a77ce2aeb43039e4c2565c9"
SOURCE_MANIFEST_SHA256 = "6d5c417c2e407aaf055151c1a3639d3b4e2ce4ad7da999f95273b6fcbf487370"
SOURCE_BATCH_MANIFEST_SHA256 = "ac6f39d1f0afe55677fbbb0ce2da1b212d22c49f18917d8fe2ed613d424c4327"
CONTEXT_MANIFEST_SHA256 = "d44a23ac674d5fd3f8e9325c80884dc1426305055b873be1da239f126056e570"
CONTENT_MANIFEST_SHA256 = "e8821e71f49c5d9709297b12f0cd7a345e2e85747e16e19ba79feea93c3354f7"
LIFE_MANIFEST_SHA256 = "e2ab03ec163b15c350795fbe5f75a279cdc5f86a3c5475db44982eddbe3ff0e1"


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
        self.context_version = None
        self.content_version = None

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

    def load_context(self):
        """Load the coordinator's extension and its complete pinned dependency tree."""
        directory = self.directory.parent.parent / "memory-context/v1"
        manifest_bytes = (directory / "manifest.json").read_bytes()
        if digest(manifest_bytes) != CONTEXT_MANIFEST_SHA256:
            raise ValueError("Memory context contract manifest hash mismatch")
        manifest = json.loads(manifest_bytes)
        if (
            manifest["package"] != "memory-context/v1"
            or manifest["version"] != "1.0.0"
            or manifest["dependencies"]
            != {"text-dialogue": MANIFEST_SHA256, "source-sync": SOURCE_MANIFEST_SHA256}
        ):
            raise ValueError("Unsupported memory context contract")
        files = {}
        for relative, expected in manifest["sha256"].items():
            path = directory / relative
            if not path.resolve().is_relative_to(directory.resolve()):
                raise ValueError("Memory context contract path escapes version directory")
            contents = path.read_bytes()
            if digest(contents) != expected:
                raise ValueError("Memory context contract file hash mismatch: " + relative)
            files[relative] = contents
        self.load_source_batches()
        schema = json.loads(files["schema.json"])
        if schema["$id"] != "https://contracts.tianshu.invalid/memory-context/v1/schema.json":
            raise ValueError("Memory context schema identifier mismatch")
        self.schemas["memory-context"] = schema
        self.registry = self.registry.with_resource(schema["$id"], Resource.from_contents(schema))
        self.context_version = manifest["version"]

    def load_content(self):
        """Verify the full one-direction Knowledge -> Life -> Common dependency graph."""
        Contracts(self.directory)
        for package, pin, version, dependencies, filename, name in (
            (
                "life-runtime/v2",
                LIFE_MANIFEST_SHA256,
                "2.0.0",
                {"text-dialogue/v1": MANIFEST_SHA256},
                "schemas/life.json",
                "life",
            ),
            (
                "knowledge-content/v1",
                CONTENT_MANIFEST_SHA256,
                "1.0.0",
                {"text-dialogue/v1": MANIFEST_SHA256, "life-runtime/v2": LIFE_MANIFEST_SHA256},
                "schema.json",
                "knowledge-content",
            ),
        ):
            directory = self.directory.parent.parent / package
            contents = (directory / "manifest.json").read_bytes()
            if digest(contents) != pin:
                raise ValueError("Content dependency manifest mismatch: " + package)
            manifest = json.loads(contents)
            if (
                manifest["package"] != package
                or manifest["version"] != version
                or manifest["dependencies"] != dependencies
            ):
                raise ValueError("Unsupported content dependency: " + package)
            for relative, expected in manifest["sha256"].items():
                path = directory / relative
                if (
                    not path.resolve().is_relative_to(directory.resolve())
                    or digest(path.read_bytes()) != expected
                ):
                    raise ValueError("Content dependency file mismatch: " + relative)
            schema = json.loads((directory / filename).read_bytes())
            expected_id = (
                "https://contracts.tianshu.invalid/"
                + package
                + "/"
                + ("life.json" if name == "life" else "schema.json")
            )
            if schema["$id"] != expected_id:
                raise ValueError("Content schema identifier mismatch")
            self.schemas[name] = schema
            self.registry = self.registry.with_resource(
                schema["$id"], Resource.from_contents(schema)
            )
        self.content_version = "1.0.0"

    def validate(self, name: str, document: dict):
        module, definition = name.split("#")
        Draft202012Validator(
            {"$ref": f"{self.schemas[module]['$id']}#/$defs/{definition}"},
            registry=self.registry,
            format_checker=FormatChecker(),
        ).validate(document)
