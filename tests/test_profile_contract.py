import shutil

import pytest

from tianshu_memory.contracts import MANIFEST_SHA256, Contracts, digest


def copy_dependency(contracts, tmp_path):
    directory = tmp_path / "text-dialogue/v1"
    shutil.copytree(contracts.directory, directory)
    return directory


def test_v1_remains_usable_without_optional_profile_package(contracts, tmp_path):
    directory = copy_dependency(contracts, tmp_path)
    loaded = Contracts(directory)
    assert loaded.profile_version is None
    assert digest((directory / "manifest.json").read_bytes()) == MANIFEST_SHA256
    with pytest.raises(FileNotFoundError):
        loaded.load_profiles()


@pytest.mark.parametrize("changed_file", ["manifest.json", "schemas/profiles.json"])
def test_profile_package_must_match_coordinator_pinned_hashes(contracts, tmp_path, changed_file):
    directory = copy_dependency(contracts, tmp_path)
    profiles = tmp_path / "profile-memory/v1"
    shutil.copytree(contracts.directory.parent.parent / "profile-memory/v1", profiles)
    loaded = Contracts(directory)
    loaded.load_profiles()
    assert loaded.profile_version == "1.0.0"
    target = profiles / changed_file
    target.write_bytes(target.read_bytes() + b"\n ")
    with pytest.raises(ValueError, match="hash mismatch"):
        Contracts(directory).load_profiles()
