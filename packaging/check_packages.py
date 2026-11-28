"""Build and verify the local portable packages through Personal Harness."""

import argparse
import hashlib
import json
import os
import plistlib
import struct
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from build import BUNDLE_NAME, MAP, PROJECT, PYTHON_RUNTIME_NAME, PYTHON_VERSION, WINDOWS_RUNTIME_SHA256


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(message)


def windows_runtime(build_root: Path) -> Path:
    return build_root / f"python-{PYTHON_VERSION}-embed-amd64.zip"


def run_source_tests() -> None:
    subprocess.run(
        [sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests", "-v"],
        cwd=PROJECT, stderr=subprocess.STDOUT, check=True,
    )
    for definition in sorted((PROJECT / "examples").glob("*.json")):
        subprocess.run([sys.executable, "-B", "-m", "harness", "validate", definition], cwd=PROJECT, check=True)


def build_packages(build_root: Path) -> Path:
    embed = windows_runtime(build_root)
    require(embed.is_file(), f"Place the official Windows runtime archive at {embed}.")
    with tempfile.TemporaryDirectory(prefix=".harness-build-", dir=build_root) as folder:
        stage = Path(folder)
        output = stage / "packages"
        env = {**os.environ, "PYINSTALLER_CONFIG_DIR": str(stage / "pyinstaller-cache")}
        builder = [sys.executable, "-B", PROJECT / "packaging/build.py"]
        subprocess.run([*builder, "macos", "--output-dir", output], cwd=PROJECT, env=env,
                       stdout=sys.stderr, check=True)
        subprocess.run([*builder, "windows", "--output-dir", output, "--python-embed", embed],
                       cwd=PROJECT, stdout=sys.stderr, check=True)
        suffix = stage.name.removeprefix(".harness-build-")
        return output.rename(build_root / f"personal-harness-{suffix}")


def verify_macos(archive: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="Harness package check ") as folder:
        workspace = Path(folder).resolve()
        subprocess.run(["/usr/bin/ditto", "-xk", archive, workspace], check=True)
        bundle = workspace / BUNDLE_NAME
        runtime = bundle / "runtime"
        versions = runtime / "_internal/Python.framework/Versions"
        frameworks = list(versions.glob("[0-9]*/Resources/Info.plist"))
        require(len(frameworks) == 1, "Expected exactly one bundled Python framework.")
        with frameworks[0].open("rb") as stream:
            require(plistlib.load(stream).get("CFBundleVersion") == PYTHON_VERSION,
                    f"Expected bundled macOS Python {PYTHON_VERSION}.")
        binaries = 0
        for path in runtime.rglob("*"):
            if path.is_symlink() or not path.is_file():
                continue
            if "Mach-O" not in subprocess.check_output(["/usr/bin/file", "-b", path], text=True):
                continue
            architectures = subprocess.check_output(["/usr/bin/lipo", "-archs", path], text=True).split()
            require(set(architectures) == {"arm64", "x86_64"}, f"Expected both Mac architectures in {path.name}.")
            dependencies = subprocess.check_output(["/usr/bin/otool", "-arch", "all", "-L", path], text=True)
            require(not any(line.strip().startswith("/Library/Frameworks/Python.framework/")
                            for line in dependencies.splitlines()), f"External Python dependency in {path.name}.")
            binaries += 1
        subprocess.run(["/usr/bin/codesign", "--verify", "--strict", runtime / "harness"], check=True)
        env = {"PATH": "/usr/bin:/bin", "HOME": os.environ["HOME"], "LANG": "en_US.UTF-8"}

        def invoke(*args: str | Path) -> dict:
            return json.loads(subprocess.check_output([bundle / "harness", *args], cwd=workspace,
                                                      env=env, text=True))

        skills = workspace / ".agents/skills"
        configured = invoke("setup-codex", "--skills-dir", skills)
        installed = skills / "personal-harness"
        require(configured["status"] == "configured" and Path(configured["skill"]) == installed,
                "The packaged Codex setup did not install the skill.")
        require((installed / "SKILL.md").read_bytes() ==
                (bundle / "codex-skill/personal-harness/SKILL.md").read_bytes(), "The installed skill differs from the ZIP.")
        require(Path((installed / "runner-location.txt").read_text(encoding="utf-8").strip()) == bundle,
                "The packaged Codex setup saved the wrong runner path.")
        require(invoke("setup-codex", "--skills-dir", skills) == configured, "Repeating Codex setup failed.")

        workflow = bundle / "examples/research-briefing.json"
        require(invoke("validate", workflow)["valid"], "The packaged CLI rejected the sample workflow.")
        run_id = invoke("start", workflow)["run_id"]
        require(invoke("resume", run_id)["status"] == "waiting_for_approval", "The sample skipped its review.")
        require(invoke("approve", run_id, "review_briefing")["status"] == "succeeded", "Sample approval failed.")
        demo = subprocess.check_output([bundle / "Start Harness.command"], cwd=workspace, env=env,
                                       input="\ny\n\n", text=True)
        require("Draft: Sample metadata review is complete." in demo and "Status: succeeded" in demo,
                "The macOS demo did not complete.")
        command = workspace / "command.json"
        command.write_text(json.dumps({"version": 1, "name": "Packaged command", "steps": [{
            "id": "echo", "type": "action", "adapter": "command", "config": {"argv": ["/bin/echo", "works"]},
        }]}), encoding="utf-8")
        run_id = invoke("start", command)["run_id"]
        require(invoke("resume", run_id, "--allow-command")["status"] == "succeeded", "Packaged command failed.")
        result = invoke("status", run_id, "--outputs")
        require(result["steps"][0]["output"]["stdout"] == "works\n", "Packaged command output differed.")
        verify_packaged_backup(bundle)
        print(f"macOS Python {PYTHON_VERSION}, {binaries} universal2 binaries, Codex setup, CLI and offline demo passed.")


def verify_packaged_backup(bundle: Path) -> None:
    bundle = bundle.resolve()
    launcher = (
        [Path(os.environ["SystemRoot"]) / "System32/cmd.exe", "/d", "/c", "harness.cmd"]
        if os.name == "nt" else [bundle / "harness"]
    )
    example = Path("examples/research-briefing.json")

    with tempfile.TemporaryDirectory(prefix="Harness migration ") as folder:
        root = Path(folder).resolve()
        original = root / "original.sqlite3"
        moved_workspace = root / "moved workspace"
        moved_workspace.mkdir()
        saved = root / "backup.zip"
        shared = root / "workflows.zip"
        restored = root / "restored"

        def report(*args: str | Path, db: Path = original) -> dict:
            result = subprocess.check_output(
                [*launcher, "--db", db, *args], cwd=bundle, text=True, encoding="utf-8", timeout=60,
            )
            return json.loads(result)

        run = report("start", example, "--workspace", bundle)["run_id"]
        pending = report("resume", run)
        require(pending["status"] == "waiting_for_approval", "Packaged workflow did not pause for review.")

        report("backup", saved, "--workflow", example)
        preview = report("inspect-backup", saved)
        require(preview["includes_state"] and preview["runs"][0]["run_id"] == run, "Backup lost the run ID.")

        result = report("restore", saved, restored, "--map-workspace", bundle, moved_workspace)
        db = Path(result["database"])
        require(not result["unavailable_workspaces"], "Backup did not restore the workspace mapping.")
        require(report("status", run, db=db)["status"] == pending["status"], "Restore lost the pending approval.")
        require(
            report("approve", run, "review_briefing", db=db)["status"] == "succeeded",
            "Restored workflow did not complete after approval.",
        )
        require(report("status", run)["status"] == pending["status"], "Approving a restored run changed the original.")

        report("export-workflows", shared, "--workflow", example)
        preview = report("inspect-backup", shared)
        require(not preview["includes_state"] and not preview["runs"], "Workflow-only export included run data.")
        received = root / "received"
        result = report("restore", shared, received)
        require(result["database"] is None, "Workflow-only restore created a database.")
        definition = received / result["workflows"][0]["path"]
        require(report("validate", definition)["valid"], "The exported workflow did not validate.")
    print("Packaged backup, workspace remapping, restored approval, and workflow export passed.")


def verify_windows(archive: Path, embed: Path) -> None:
    with embed.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    require(digest == WINDOWS_RUNTIME_SHA256, "The Windows reference runtime checksum changed.")
    prefix = f"{BUNDLE_NAME}/"
    runtime = f"{prefix}runtime/"
    pth = f"{PYTHON_RUNTIME_NAME}._pth"
    with zipfile.ZipFile(archive) as bundle, zipfile.ZipFile(embed) as upstream:
        for name in upstream.namelist():
            if name != pth:
                require(bundle.read(runtime + name) == upstream.read(name), f"Bundled Windows file differs: {name}.")
        require(bundle.read(runtime + pth).splitlines() == [f"{PYTHON_RUNTIME_NAME}.zip".encode(), b".", b".."],
                "The Windows runtime search paths differ.")
        for name in ("python.exe", f"{PYTHON_RUNTIME_NAME}.dll", "_sqlite3.pyd"):
            data = bundle.read(runtime + name)
            pe = struct.unpack_from("<I", data, 0x3C)[0]
            require(data[:2] == b"MZ" and data[pe:pe + 6] == b"PE\0\0\x64\x86", f"Expected AMD64 PE: {name}.")
        for name in ("harness.cmd", "Start Harness.cmd", "launch.py", "harness/windows_process.py",
                     "harness/windows_worker.py", "harness/codex_setup.py", "harness/backup.py",
                     "codex-skill/personal-harness/SKILL.md",
                     "examples/research-briefing.json"):
            require(prefix + name in bundle.namelist(), f"Missing Windows package file: {name}.")
        for name in bundle.namelist():
            if name.endswith(".py"):
                compile(bundle.read(name), name, "exec")
    print(f"Windows Python {PYTHON_VERSION} x64, embedded runtime and package structure passed. Native execution pending.")


def verify_packages(output: Path, embed: Path) -> None:
    for name in MAP.values():
        archive = output / name
        with zipfile.ZipFile(archive) as bundle:
            require(bundle.testzip() is None, f"Invalid ZIP: {name}.")
        with archive.open("rb") as stream:
            print(f"{hashlib.file_digest(stream, 'sha256').hexdigest()}  {archive}")
    verify_macos(output / MAP["macos"])
    verify_windows(output / MAP["windows"], embed)


def main() -> None:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("step", choices=("test", "build", "verify"))
    args = cli.parse_args()
    build_root = Path(os.environ.get("HARNESS_BUILD_DIR", PROJECT / "dist")).resolve()
    if args.step == "test":
        run_source_tests()
    elif args.step == "build":
        print(build_packages(build_root))
    else:
        output = Path(json.load(sys.stdin)["build_packages"]["stdout"].strip())
        verify_packages(output, windows_runtime(output.parent))


if __name__ == "__main__":
    main()
