"""SQLite state and per-run locking."""

import errno
import json
import os
import re
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path

from .workflow import JsonObject, Workflow, WorkflowError, json_text

if os.name == "nt":
    import msvcrt
else:
    import fcntl

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    definition TEXT NOT NULL,
    workspace TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS steps (
    run_id TEXT NOT NULL REFERENCES runs(id),
    position INTEGER NOT NULL,
    step_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    output TEXT,
    reason TEXT,
    PRIMARY KEY (run_id, step_id),
    UNIQUE (run_id, position)
);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id),
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    step_id TEXT,
    attempt INTEGER,
    details TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_by_run ON events(run_id, seq);
"""


def timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class Store:
    def __init__(self, path: Path, *, create: bool = False):
        self.path = path.resolve()
        if create:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(fd)
            with closing(self.connect()) as db:
                db.executescript(SCHEMA)
        elif not self.path.is_file():
            raise WorkflowError(f"State database does not exist: {self.path}")

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        return db

    @contextmanager
    def run_lock(self, run_id: str) -> Iterator[None]:
        self.run(run_id)
        directory = self.path.with_name(self.path.name + ".locks")
        directory.mkdir(mode=0o700, exist_ok=True)
        fd = os.open(directory / run_id, os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(fd, "r+b") as lock:
            try:
                if os.name == "nt":
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                if error.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise
                raise WorkflowError(f"Run {run_id} is already being advanced") from error
            yield

    def create(self, workflow: Workflow, workspace: Path) -> str:
        run_id = uuid.uuid4().hex
        with closing(self.connect()) as db, db:
            db.execute(
                "INSERT INTO runs VALUES (?, ?, ?, ?, ?)",
                (run_id, workflow.name, workflow.snapshot, str(workspace), timestamp()),
            )
            db.executemany(
                "INSERT INTO steps (run_id, position, step_id) VALUES (?, ?, ?)",
                [(run_id, index, step.id) for index, step in enumerate(workflow.steps)],
            )
            self._event(db, run_id, "run_created")
        return run_id

    def run(self, run_id: str) -> sqlite3.Row:
        with closing(self.connect()) as db:
            row = db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if not row or not re.fullmatch(r"[0-9a-f]{32}", run_id):
            raise WorkflowError(f"Unknown run: {run_id}")
        return row

    def status(self, run_id: str, *, include_outputs: bool = False) -> JsonObject:
        run = self.run(run_id)
        with closing(self.connect()) as db:
            rows = db.execute(
                "SELECT step_id, status, attempts, reason, output FROM steps "
                "WHERE run_id = ? ORDER BY position",
                (run_id,),
            ).fetchall()
        steps = []
        for row in rows:
            step = dict(row)
            output = step.pop("output")
            if include_outputs and output is not None:
                step["output"] = json.loads(output)
            steps.append(step)
        current = next((step for step in steps if step["status"] != "succeeded"), None)
        status = current["status"] if current else "succeeded"
        return {
            "run_id": run_id,
            "workflow": run["name"],
            "created_at": run["created_at"],
            "status": "ready" if status == "pending" else status,
            "current_step": current["step_id"] if current else None,
            "steps": steps,
        }

    def events(self, run_id: str) -> Iterator[JsonObject]:
        self.run(run_id)
        with closing(self.connect()) as db:
            for row in db.execute("SELECT * FROM events WHERE run_id = ? ORDER BY seq", (run_id,)):
                event = dict(row)
                event["details"] = json.loads(event["details"])
                yield event

    def transition(
        self,
        run_id: str,
        step_id: str,
        old: str,
        new: str,
        kind: str,
        *,
        start_attempt: bool = False,
        output: str | None = None,
        reason: str | None = None,
        details: JsonObject | None = None,
    ) -> None:
        with closing(self.connect()) as db, db:
            row = db.execute(
                "UPDATE steps SET status = ?, attempts = attempts + ?, output = ?, reason = ? "
                "WHERE run_id = ? AND step_id = ? AND status = ? RETURNING attempts",
                (new, int(start_attempt), output, reason, run_id, step_id, old),
            ).fetchone()
            if row is None:
                raise WorkflowError(f"Step {step_id} changed while advancing the run")
            metadata = dict(details or {})
            if reason:
                metadata["reason"] = reason
            self._event(db, run_id, kind, step_id, row["attempts"] or None, metadata)
            if new in {"failed", "rejected"}:
                self._event(db, run_id, f"run_{new}")
            elif new == "succeeded":
                unfinished = db.execute(
                    "SELECT 1 FROM steps WHERE run_id = ? AND status != 'succeeded' LIMIT 1",
                    (run_id,),
                ).fetchone()
                if unfinished is None:
                    self._event(db, run_id, "run_succeeded")

    @staticmethod
    def _event(
        db: sqlite3.Connection,
        run_id: str,
        kind: str,
        step_id: str | None = None,
        attempt: int | None = None,
        details: JsonObject | None = None,
    ) -> None:
        db.execute(
            "INSERT INTO events (run_id, at, kind, step_id, attempt, details) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, timestamp(), kind, step_id, attempt, json_text(details or {})),
        )
