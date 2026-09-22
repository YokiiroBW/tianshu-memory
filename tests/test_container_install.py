"""Offline lock regressions using uv; fresh full installation is a separate explicit command."""

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "container_build", ROOT / "infra/container/build_install.py"
)
BUILD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUILD)


@pytest.fixture
def project(tmp_path):
    for name in ("pyproject.toml", "uv.lock"):
        shutil.copyfile(ROOT / name, tmp_path / name)
    return tmp_path


def export(project, mode):
    # CI/local acceptance supplies the fixed build interpreter. Normal product runs can use uv
    # from PATH, but may not claim that this alone exercised the pinned builder version.
    tool_python = os.environ.get("TIANSHU_BUILD_TOOL_PYTHON")
    command = [tool_python, "-I", "-m", "uv"] if tool_python else ["uv"]
    return subprocess.run(
        [
            *command,
            "--no-config",
            "--offline",
            "export",
            mode,
            "--no-dev",
            "--no-emit-project",
            "--python",
            sys.executable,
            "--output-file",
            str(project / "requirements.txt"),
        ],
        cwd=project,
        env=BUILD.environment(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )


def test_current_lock_exports_without_mutating_inputs(project):
    before = {name: BUILD.digest(project / name) for name in ("pyproject.toml", "uv.lock")}
    result = export(project, "--locked")
    assert result.returncode == 0, result.stderr
    assert before == {name: BUILD.digest(project / name) for name in before}
    requirements = (project / "requirements.txt").read_text(encoding="utf-8")
    assert "fastapi==" in requirements and "--hash=sha256:" in requirements
    assert "pytest==" not in requirements and "mcp==" not in requirements


def test_stale_metadata_is_rejected_but_frozen_would_accept_it(project):
    metadata = project / "pyproject.toml"
    # This range excludes the locked httpx 0.28.1. uv may report missing offline resolver
    # metadata before its stale-lock diagnostic; either must refuse export. The unchanged-lock
    # control above succeeds offline.
    metadata.write_text(
        metadata.read_text(encoding="utf-8").replace("httpx>=0.28,<1", "httpx>=0.27,<0.28"),
        encoding="utf-8",
    )
    before = BUILD.digest(project / "uv.lock")
    locked = export(project, "--locked")
    assert locked.returncode != 0
    assert "lockfile" in locked.stderr or "No solution found" in locked.stderr
    assert not (project / "requirements.txt").exists()
    assert BUILD.digest(project / "uv.lock") == before
    frozen = export(project, "--frozen")
    assert frozen.returncode == 0, frozen.stderr
    assert "httpx==0.28.1" in (project / "requirements.txt").read_text(encoding="utf-8")
    assert BUILD.digest(project / "uv.lock") == before


def test_absent_lock_is_not_created_by_export(project):
    (project / "uv.lock").unlink()
    result = export(project, "--locked")
    assert result.returncode != 0
    assert not (project / "uv.lock").exists()
    assert not (project / "requirements.txt").exists()


def test_existing_output_cannot_hide_missing_build_tools(tmp_path, monkeypatch):
    monkeypatch.setattr(BUILD.sys, "version_info", (3, 12))
    output = tmp_path / "existing"
    output.mkdir()
    sentinel = output / "state"
    sentinel.write_bytes(b"preserved")
    with pytest.raises(FileExistsError):
        BUILD.build(ROOT, output)
    assert sentinel.read_bytes() == b"preserved"
    assert list(output.iterdir()) == [sentinel]


def test_python_path_and_index_overrides_are_not_build_inputs(monkeypatch):
    for name in ("PYTHONPATH", "PYTHONHOME", "PIP_INDEX_URL", "UV_FROZEN", "VIRTUAL_ENV"):
        monkeypatch.setenv(name, "synthetic-untrusted-setting")
    env = BUILD.environment()
    assert (
        not {"PYTHONPATH", "PYTHONHOME", "PIP_INDEX_URL", "UV_FROZEN", "VIRTUAL_ENV"} & env.keys()
    )
    assert env["PIP_CONFIG_FILE"] == os.devnull
