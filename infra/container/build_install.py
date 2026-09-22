"""Shared Docker/local build path. All generated state stays in a new explicit directory."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path


def python_in(directory):
    return directory / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def environment():
    # Neither a caller's activated environment nor package-index configuration is a build input.
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PIP_", "UV_", "PYTHON"))
        and key not in {"VIRTUAL_ENV", "CONDA_PREFIX"}
    }
    env.update(
        PYTHONDONTWRITEBYTECODE="1",
        PIP_CONFIG_FILE=os.devnull,
        UV_PYTHON_DOWNLOADS="never",
    )
    return env


def run(arguments, cwd, env):
    return subprocess.run(
        [str(item) for item in arguments],
        cwd=cwd,
        env=env,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
    ).stdout


def build(source, output):
    if sys.version_info[:2] != (3, 12):
        raise ValueError("Build verification requires Python 3.12 (image: 3.12.11).")
    source, output = source.resolve(), output.resolve()
    if output == source or output in source.parents:
        raise ValueError("Output must not contain the source directory.")
    # Never reuse an environment whose preinstalled tools could hide a missing dependency.
    output.mkdir(parents=True, exist_ok=False)
    env = environment()
    env["UV_CACHE_DIR"] = str(output / "uv-cache")
    inputs = [source / "pyproject.toml", source / "uv.lock"]
    inputs_before = {path.name: digest(path) for path in inputs}
    tools_dir, runtime = output / "tools", output / "venv"
    venv.EnvBuilder(with_pip=True).create(tools_dir)
    venv.EnvBuilder(with_pip=False).create(runtime)
    tool_python, runtime_python = python_in(tools_dir), python_in(runtime)
    pip = [tool_python, "-I", "-m", "pip", "--isolated", "--disable-pip-version-check"]
    build_requirements = source / "infra/container/build-requirements.txt"
    print("Installing hash-pinned build tools in an isolated environment", flush=True)
    run(
        [
            *pip,
            "install",
            "--index-url",
            "https://pypi.org/simple",
            "--no-cache-dir",
            "--only-binary=:all:",
            "--require-hashes",
            "-r",
            build_requirements,
        ],
        output,
        env,
    )
    uv = [tool_python, "-I", "-m", "uv", "--no-config", "--no-progress"]
    requirements = output / "requirements.txt"
    print("Checking lock freshness and exporting runtime dependencies", flush=True)
    run(
        [
            *uv,
            "export",
            "--locked",
            "--no-dev",
            "--no-emit-project",
            "--no-editable",
            "--no-header",
            "--python",
            runtime_python,
            "--output-file",
            requirements,
        ],
        source,
        env,
    )
    if inputs_before != {path.name: digest(path) for path in inputs}:
        raise ValueError("Build changed pyproject.toml or uv.lock.")
    print("Building the application wheel with the explicit setuptools backend", flush=True)
    wheels = output / "wheels"
    project = output / "project"
    project.mkdir()
    shutil.copyfile(source / "pyproject.toml", project / "pyproject.toml")
    source_hashes = {}
    for module in sorted((source / "src/tianshu_memory").rglob("*.py")):
        relative = module.relative_to(source)
        target = project / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(module, target)
        source_hashes[relative.as_posix()] = digest(module)
    run(
        [
            *pip,
            "wheel",
            "--no-deps",
            "--no-build-isolation",
            "--no-index",
            "--wheel-dir",
            wheels,
            project,
        ],
        output,
        env,
    )
    (wheel,) = wheels.glob("tianshu_memory-*.whl")
    target_pip = [*pip, "--python", runtime_python]
    print("Installing locked wheels into the clean, pip-free runtime environment", flush=True)
    run(
        [
            *target_pip,
            "install",
            "--index-url",
            "https://pypi.org/simple",
            "--no-cache-dir",
            "--only-binary=:all:",
            "--require-hashes",
            "-r",
            requirements,
        ],
        output,
        env,
    )
    run([*target_pip, "install", "--no-deps", "--no-index", wheel], output, env)
    check = run([*target_pip, "check"], output, env).strip()
    installed = json.loads(
        run(
            [
                runtime_python,
                "-I",
                "-c",
                "import importlib.metadata as m,json; "
                "print(json.dumps({d.metadata['Name'].lower().replace('_','-'):d.version "
                "for d in m.distributions()}))",
            ],
            output,
            env,
        )
    )
    # pip's parser evaluates the exported lock's markers on the actual runtime platform.
    expected = json.loads(
        run(
            [
                tool_python,
                "-I",
                "-c",
                "import json,sys; from pip._vendor.packaging.requirements import Requirement; "
                "rows=[Requirement(line.strip().removesuffix(chr(92))) for line in "
                "open(sys.argv[1],encoding='utf-8') if line and line[0].isalnum()]; "
                "print(json.dumps({r.name.lower().replace('_','-'):next(iter(r.specifier)).version "
                "for r in rows if r.marker is None or r.marker.evaluate()}))",
                requirements,
            ],
            output,
            env,
        )
    )
    actual_dependencies = {
        name: version for name, version in installed.items() if name != "tianshu-memory"
    }
    if actual_dependencies != expected:
        raise ValueError("Installed runtime packages differ from the exported lock.")
    forbidden = {"pip", "setuptools", "wheel", "uv", "pytest", "ruff", "mcp"}
    if forbidden & installed.keys():
        raise ValueError("Build, dev or optional MCP tools leaked into the runtime.")
    cli = runtime / ("Scripts/tianshu-memory.exe" if os.name == "nt" else "bin/tianshu-memory")
    for command in (
        [cli, "--help"],
        [cli, "--config", "unused-synthetic.json", "serve", "--help"],
        [runtime_python, "-I", "-m", "tianshu_memory.knowledge_cli", "--help"],
        [
            runtime_python,
            "-I",
            "-m",
            "tianshu_memory.knowledge_cli",
            "--config",
            "unused-synthetic.json",
            "serve",
            "--help",
        ],
    ):
        if "usage:" not in run(command, output, env):
            raise ValueError("An installed CLI did not display help.")
    location = run(
        [runtime_python, "-I", "-c", "import tianshu_memory; print(tianshu_memory.__file__)"],
        output,
        env,
    ).strip()
    if not Path(location).resolve().is_relative_to(runtime):
        raise ValueError("Import resolved outside the installed runtime environment.")
    evidence = {
        "python": sys.version,
        "platform": sys.platform,
        "uv": run([*uv, "--version"], output, env).strip(),
        "inputs_sha256": {
            **inputs_before,
            "build-requirements.txt": digest(build_requirements),
            "build_install.py": digest(Path(__file__)),
        },
        "requirements_sha256": digest(requirements),
        "wheel_sha256": digest(wheel),
        "source_sha256": hashlib.sha256(
            json.dumps(source_hashes, sort_keys=True).encode()
        ).hexdigest(),
        "installed": installed,
        "pip_check": check,
        "cli_help_checks": 4,
        "installed_import": location,
        "runtime_has_build_tools": False,
        "container_execution": "not_tested_by_this_script",
    }
    (output / "installation-evidence.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(evidence, ensure_ascii=False, indent=2), flush=True)
    return evidence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        build(args.source, args.output)
    except subprocess.CalledProcessError as error:
        print(error.stdout, file=sys.stderr)
        print(error.stderr, file=sys.stderr)
        raise SystemExit(error.returncode) from error


if __name__ == "__main__":
    main()
