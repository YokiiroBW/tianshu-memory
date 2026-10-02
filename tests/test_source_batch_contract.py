"""The compatible batching package is locally immutable and dependency-bound."""

import shutil

import pytest

from tianshu_memory.contracts import SOURCE_BATCH_MANIFEST_SHA256, Contracts, digest


def test_published_batch_extension_keeps_original_owner_contracts(contracts):
    loaded = Contracts(contracts.directory)
    loaded.load_source_batches()
    directory = loaded.directory.parent.parent / "source-sync-batch/v1"
    assert digest((directory / "manifest.json").read_bytes()) == SOURCE_BATCH_MANIFEST_SHA256
    assert loaded.source_batch_version == loaded.source_version == "1.0.0"
    assert set(loaded.schemas) >= {"sync-sources", "sync-workflow", "sync-batch", "profiles"}
    assert not (directory / "__pycache__").exists()


@pytest.mark.parametrize(
    "package,relative",
    [
        ("source-sync-batch", "manifest.json"),
        ("source-sync-batch", "schemas/batch.json"),
        ("source-sync-batch", "rules.py"),
        ("source-sync", "schemas/sources.json"),
        ("profile-memory", "schemas/profiles.json"),
        ("text-dialogue", "schemas/common.json"),
    ],
)
def test_batch_extension_reverifies_package_and_all_dependencies(
    contracts, tmp_path, package, relative
):
    for name in ("text-dialogue", "profile-memory", "source-sync", "source-sync-batch"):
        shutil.copytree(contracts.directory.parent.parent / name, tmp_path / name)
    loaded = Contracts(tmp_path / "text-dialogue/v1")
    target = tmp_path / package / "v1" / relative
    target.write_bytes(target.read_bytes() + b"\n ")
    with pytest.raises(ValueError, match="hash mismatch"):
        loaded.load_source_batches()
    assert loaded.source_batch_version is None and loaded.source_batch_rules is None
