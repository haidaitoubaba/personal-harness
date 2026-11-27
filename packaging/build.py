"""Build one portable Personal Harness archive from the current source."""

import argparse
import hashlib
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
PYTHON_VERSION = "3.14.10"
PYTHON_RUNTIME_NAME = "python" + "".join(PYTHON_VERSION.split(".")[:2])
WINDOWS_RUNTIME_SHA256 = "90905e64a840557d8c1bc4f5b74db3d7ee85e3ffb217611cac1c7813acea4159"
BUNDLE_NAME = "Personal Harness"
MAP = {"windows": "Personal-Harness-Windows-x64.zip", "macos": "Personal-Harness-macOS.zip"}


def write_launcher(path: Path, script: str, *, windows: bool = False) -> None:
    path.write_text(script, encoding="ascii", newline="\r\n" if windows else "\n")
    if not windows:
        path.chmod(0o755)


def stage_common(bundle: Path) -> None:
    bundle.mkdir()
    shutil.copytree(PROJECT / "examples", bundle / "examples")
    shutil.copy2(PROJECT / "packaging" / "START-HERE.txt", bundle)
    shutil.copy2(PROJECT / "packaging" / "THIRD-PARTY-NOTICES.txt", bundle)


def archive_windows(bundle: Path, embed: Path, output: Path) -> None:
    with embed.open("rb") as archive:
        actual = hashlib.file_digest(archive, "sha256").hexdigest()
    if actual != WINDOWS_RUNTIME_SHA256:
        raise SystemExit(f"Windows Python archive does not match the approved {PYTHON_VERSION} AMD64 checksum.")
    runtime = bundle / "runtime"
    with zipfile.ZipFile(embed) as archive:
        archive.extractall(runtime)
    (runtime / f"{PYTHON_RUNTIME_NAME}._pth").write_text(f"{PYTHON_RUNTIME_NAME}.zip\n.\n..\n", encoding="ascii")
    shutil.copytree(PROJECT / "harness", bundle / "harness", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy2(PROJECT / "packaging" / "launcher.py", bundle / "launch.py")
    write_launcher(bundle / "harness.cmd", '\n'.join([
        '@echo off',
        'setlocal',
        '"%~dp0runtime\\python.exe" -B -X utf8 "%~dp0launch.py" %*',
        'exit /b %errorlevel%',
        '',
    ]), windows=True)
    write_launcher(bundle / "Start Harness.cmd", '\n'.join([
        '@echo off',
        'setlocal',
        'pushd "%~dp0" || exit /b 1',
        'call "%~dp0harness.cmd" --guided-demo',
        'set "result=%errorlevel%"',
        'popd',
        'pause',
        'exit /b %result%',
        '',
    ]), windows=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(bundle.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(bundle.parent))


def archive_macos(bundle: Path, output: Path) -> None:
    if sys.platform != "darwin":
        raise SystemExit("The macOS bundle requires a macOS Python and PyInstaller.")
    if platform.python_version() != PYTHON_VERSION:
        raise SystemExit(f"The macOS bundle requires Python {PYTHON_VERSION} (universal2).")
    import PyInstaller

    staging = bundle.parent
    build_env = os.environ.copy()
    subprocess.run([
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir",
        "--name", "harness", "--target-architecture", "universal2", "--paths", str(PROJECT),
        "--distpath", str(staging / "dist"), "--workpath", str(staging / "build"),
        "--specpath", str(staging), str(PROJECT / "packaging" / "launcher.py"),
    ], cwd=PROJECT, env=build_env, check=True)
    shutil.move(staging / "dist" / "harness", bundle / "runtime")
    licenses = bundle / "licenses"
    licenses.mkdir()
    python_license = Path(sys.base_prefix) / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "LICENSE.txt"
    shutil.copy2(python_license, licenses / "Python-LICENSE.txt")
    installer_license = Path(PyInstaller.__file__).parent.parent.glob(
        "pyinstaller-*.dist-info/licenses/COPYING.txt"
    )
    shutil.copy2(next(installer_license), licenses / "PyInstaller-COPYING.txt")
    write_launcher(bundle / "harness", '\n'.join([
        '#!/bin/bash',
        'set -eu',
        'here="$(cd -- "$(dirname -- "$0")" && pwd)"',
        'exec "$here/runtime/harness" "$@"',
        '',
    ]))
    write_launcher(bundle / "Start Harness.command", '\n'.join([
        '#!/bin/bash',
        'here="$(cd -- "$(dirname -- "$0")" && pwd)" || exit 1',
        'cd "$here" || exit 1',
        '"$here/harness" --guided-demo',
        'result=$?',
        'read -r -p "Press Enter to close. "',
        'exit "$result"',
        '',
    ]))
    subprocess.run(
        ["ditto", "-c", "-k", "--norsrc", "--keepParent", str(bundle), str(output)], check=True,
    )


def main() -> None:
    cli = argparse.ArgumentParser()
    cli.add_argument("platform", choices=MAP)
    cli.add_argument("--output-dir", required=True, type=Path)
    cli.add_argument("--python-embed", type=Path)
    args = cli.parse_args()
    if args.platform == "windows" and args.python_embed is None:
        cli.error("--python-embed is required for a Windows build")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    output = args.output_dir.resolve() / MAP[args.platform]
    if output.exists():
        raise SystemExit(f"Output already exists: {output}")
    with tempfile.TemporaryDirectory(prefix="harness-build-", dir=args.output_dir) as directory:
        bundle = Path(directory).resolve() / BUNDLE_NAME
        stage_common(bundle)
        archive = bundle.parent / output.name
        if args.platform == "windows":
            archive_windows(bundle, args.python_embed, archive)
        else:
            archive_macos(bundle, archive)
        archive.rename(output)
    print(output)


if __name__ == "__main__":
    main()
