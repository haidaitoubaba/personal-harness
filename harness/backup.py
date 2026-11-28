"""Create and restore portable bundles of workflows and local run state."""

import hashlib
import os
import re
import shutil
import sqlite3
import tempfile
import zipfile
import zlib
from collections.abc import Iterator, Mapping, Sequence
from contextlib import closing, contextmanager
from pathlib import Path
from typing import BinaryIO

from .store import timestamp
from .workflow import (
    Action, Adapter, JsonObject, WorkflowError, json_text, load_json, object_with, parse_workflow,
)

MANIFEST = "manifest.json"
STATE = ".harness/state.sqlite3"
FORMAT = "personal-harness-backup"
VERSION = 1
WORKFLOW_PATH = re.compile(r"workflows/[0-9]+-[a-z0-9_-]{1,64}\.json")
SHA256 = re.compile(r"[0-9a-f]{64}")
MAX_MANIFEST_BYTES = 1024 * 1024


def _workflow_info(source: str, adapters: Mapping[str, Adapter]) -> JsonObject:
    workflow = parse_workflow(source, adapters, allow_unknown_adapters=True)
    return {
        "name": workflow.name,
        "adapters": sorted({step.adapter for step in workflow.steps if isinstance(step, Action)}),
    }


def _absolute_target(path: Path) -> Path:
    return path.parent.resolve() / path.name


def _require_new(path: Path) -> None:
    if path.exists() or path.is_symlink():
        raise WorkflowError(f"Destination already exists: {path}")


def _connect(path: Path, *, writable: bool = False) -> sqlite3.Connection:
    mode = "rw" if writable else "ro"
    db = sqlite3.connect(path.resolve().as_uri() + f"?mode={mode}", uri=True)
    db.execute("PRAGMA trusted_schema = OFF")
    return db


def _snapshot(source: Path, destination: Path) -> None:
    if not source.is_file():
        raise WorkflowError(f"State database does not exist: {source.resolve()}")
    destination.parent.mkdir(mode=0o700)
    destination.touch(mode=0o600, exist_ok=False)
    with closing(_connect(source)) as live, closing(_connect(destination, writable=True)) as copy:
        live.execute("BEGIN")
        # Pin a committed snapshot, including changes in the source's WAL.
        live.execute("SELECT name FROM sqlite_schema LIMIT 1").fetchone()
        live.backup(copy)
        copy.execute("PRAGMA journal_mode = DELETE")


def _state_runs(path: Path, adapters: Mapping[str, Adapter]) -> list[JsonObject]:
    with closing(_connect(path)) as db:
        tables = {row[1] for row in db.execute("PRAGMA table_list") if row[2] == "table"}
        if not {"runs", "steps", "events"} <= tables:
            raise WorkflowError("Bundle does not contain a Harness state database")
        if db.execute("SELECT 1 FROM sqlite_schema WHERE type = 'trigger'").fetchone():
            raise WorkflowError("Harness state database contains unexpected triggers")
        if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise WorkflowError("Harness state database failed its integrity check")
        if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise WorkflowError("Harness state database has inconsistent run references")
        runs = []
        for run_id, definition, workspace in db.execute(
            "SELECT id, definition, workspace FROM runs ORDER BY created_at, id"
        ):
            if not isinstance(run_id, str) or not re.fullmatch(r"[0-9a-f]{32}", run_id):
                raise WorkflowError("Harness state database has an invalid run ID")
            if type(workspace) is not str or not workspace or "\0" in workspace:
                raise WorkflowError(f"Run {run_id} has an invalid workspace")
            if type(definition) is not str:
                raise WorkflowError(f"Run {run_id} has an invalid workflow definition")
            runs.append({"run_id": run_id, "workspace": workspace} | _workflow_info(definition, adapters))
        return runs


def _write_private(path: Path) -> BinaryIO:
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0), 0o600)
    return os.fdopen(fd, "wb")


def _file_hash(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _summary(manifest: JsonObject, workflows: list[JsonObject], runs: list[JsonObject]) -> JsonObject:
    return {
        "kind": manifest["kind"],
        "version": manifest["version"],
        "created_at": manifest["created_at"],
        "includes_state": STATE in manifest["files"],
        "workflows": workflows,
        "runs": runs,
        "required_adapters": sorted({adapter for item in workflows + runs for adapter in item["adapters"]}),
    }


def create_bundle(
    target: Path,
    definitions: Sequence[Path],
    adapters: Mapping[str, Adapter],
    *,
    database: Path | None = None,
) -> JsonObject:
    """Back up state, or export only the explicitly selected workflow files."""
    if database is None and not definitions:
        raise WorkflowError("Select at least one --workflow to export")
    target = _absolute_target(target)
    _require_new(target)

    with tempfile.TemporaryDirectory(prefix="harness-backup-") as tmp:
        staging = Path(tmp)
        runs = []
        if database is not None:
            _snapshot(database, staging / STATE)
            runs = _state_runs(staging / STATE, adapters)
        workflows = []
        digits = max(3, len(str(len(definitions))))
        for index, source in enumerate(definitions, start=1):
            label = re.sub(r"[^a-z0-9_-]+", "-", source.stem.lower()).strip("-")[:64] or "workflow"
            name = f"workflows/{index:0{digits}d}-{label}.json"
            definition = source.read_bytes()
            info = _workflow_info(definition.decode("utf-8"), adapters)
            # Preserve the selected definition as an editable JSON file.
            dest = staging / name
            dest.parent.mkdir(exist_ok=True)
            dest.write_bytes(definition)
            workflows.append({"path": name} | info)

        names = [item["path"] for item in workflows]
        if database is not None:
            names.append(STATE)
        manifest = {
            "format": FORMAT,
            "version": VERSION,
            "kind": "backup" if database is not None else "workflow-export",
            "created_at": timestamp(),
            "files": {name: _file_hash(staging / name) for name in names},
        }
        manifest_data = json_text(manifest).encode("utf-8")
        if len(manifest_data) > MAX_MANIFEST_BYTES:
            raise WorkflowError("Too many workflow files for one bundle")

        # Stage the complete ZIP beside the target, then reserve the destination
        # exclusively before we replace the placeholder with the finished file.
        with tempfile.TemporaryDirectory(prefix=".harness-backup-", dir=target.parent) as publication:
            archive_path = Path(publication) / "bundle.zip"
            with _write_private(archive_path) as stream, zipfile.ZipFile(
                stream, "w", compression=zipfile.ZIP_DEFLATED
            ) as archive:
                archive.writestr(MANIFEST, manifest_data)
                for name in names:
                    archive.write(staging / name, name)

            _write_private(target).close()
            try:
                archive_path.replace(target)
            except BaseException:
                target.unlink(missing_ok=True)
                raise

    return {"archive": str(target)} | _summary(manifest, workflows, runs)


@contextmanager
def _open_bundle(source: Path) -> Iterator[zipfile.ZipFile]:
    try:
        with zipfile.ZipFile(source) as archive:
            yield archive
    except (zipfile.BadZipFile, zipfile.LargeZipFile, NotImplementedError, RuntimeError, zlib.error) as error:
        raise WorkflowError("Cannot read the Harness bundle") from error


def _read_manifest(archive: zipfile.ZipFile) -> JsonObject:
    entries = archive.namelist()
    if len(entries) != len(set(entries)) or MANIFEST not in entries:
        raise WorkflowError("Bundle must have one manifest and unique archive entries")
    if archive.getinfo(MANIFEST).file_size > MAX_MANIFEST_BYTES:
        raise WorkflowError("Bundle manifest is too large")
    manifest = object_with(
        load_json(archive.read(MANIFEST).decode("utf-8")),
        "manifest", {"format", "version", "kind", "created_at", "files"},
    )
    if manifest["format"] != FORMAT or type(manifest["version"]) is not int or manifest["version"] != VERSION:
        raise WorkflowError("Unsupported Harness bundle format or version")
    kind = manifest["kind"]
    files = manifest["files"]
    if type(files) is not dict or not files or files.keys() != set(entries) - {MANIFEST}:
        raise WorkflowError("Bundle contents do not match its manifest")
    if kind not in ("backup", "workflow-export") or (STATE in files) != (kind == "backup"):
        raise WorkflowError("Bundle type does not match its contents")
    for name, digest in files.items():
        if (name != STATE and not WORKFLOW_PATH.fullmatch(name)) or type(digest) is not str or not SHA256.fullmatch(digest):
            raise WorkflowError("Bundle has an invalid file path or checksum")
    if type(manifest["created_at"]) is not str:
        raise WorkflowError("Bundle has an invalid timestamp")
    return manifest


def _unpack(source: Path, staging: Path, adapters: Mapping[str, Adapter]) -> JsonObject:
    with _open_bundle(source) as archive:
        manifest = _read_manifest(archive)
        workflows = []
        runs = []
        for name, expected_hash in manifest["files"].items():
            path = staging / name
            path.parent.mkdir(mode=0o700, exist_ok=True)
            with archive.open(name) as payload, _write_private(path) as stream:
                shutil.copyfileobj(payload, stream)
            if _file_hash(path) != expected_hash:
                raise WorkflowError(f"Bundle checksum failed for {name}")
            if name == STATE:
                runs = _state_runs(path, adapters)
            else:
                workflows.append({"path": name} | _workflow_info(path.read_text(encoding="utf-8"), adapters))
    return _summary(manifest, workflows, runs)


def inspect_bundle(source: Path, adapters: Mapping[str, Adapter]) -> JsonObject:
    with tempfile.TemporaryDirectory(prefix="harness-inspect-") as directory:
        return _unpack(source, Path(directory), adapters)


def restore_bundle(
    source: Path,
    target: Path,
    adapters: Mapping[str, Adapter],
    *,
    workspace_mappings: Sequence[tuple[str, Path]] = (),
) -> JsonObject:
    """Verify and restore to a new directory without running any workflow."""
    target = _absolute_target(target)
    _require_new(target)
    with tempfile.TemporaryDirectory(prefix=".harness-restore-", dir=target.parent) as directory:
        staging = Path(directory)
        summary = _unpack(source, staging, adapters)
        runs = summary["runs"]
        old_paths = {run["workspace"] for run in runs}
        mappings = {}
        for old, new in workspace_mappings:
            if old not in old_paths:
                raise WorkflowError(f"No run in this bundle uses workspace: {old}")
            if old in mappings:
                raise WorkflowError(f"Workspace was mapped more than once: {old}")
            new = new.resolve()
            if not new.is_dir():
                raise WorkflowError(f"Mapped workspace must be an existing directory: {new}")
            mappings[old] = str(new)

        if mappings:
            with closing(_connect(staging / STATE, writable=True)) as db, db:
                db.executemany(
                    "UPDATE runs SET workspace = ? WHERE id = ?",
                    [(mappings[run["workspace"]], run["run_id"]) for run in runs if run["workspace"] in mappings],
                )
            for run in runs:
                run["workspace"] = mappings.get(run["workspace"], run["workspace"])

        paths = {run["workspace"] for run in runs}
        summary["unavailable_workspaces"] = sorted(
            path for path in paths if not (Path(path).is_absolute() and Path(path).is_dir())
        )

        # mkdir reserves the destination exclusively on Windows and POSIX.
        # Only validated files are published, and an existing directory is never replaced.
        target.mkdir(mode=0o700)
        published = []
        try:
            for folder in staging.iterdir():
                destination = target / folder.name
                folder.rename(destination)
                published.append(destination)
        except BaseException:
            for path in published:
                shutil.rmtree(path)
            target.rmdir()
            raise

    return {
        "destination": str(target),
        "database": str(target / STATE) if summary["includes_state"] else None,
    } | summary
