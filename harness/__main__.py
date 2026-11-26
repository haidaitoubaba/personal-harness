"""Command-line entry point for the local Harness."""

import argparse
import sqlite3
import sys
from pathlib import Path

from .adapters import built_in_adapters
from .runner import Runner
from .store import Store
from .workflow import WorkflowError, json_text, parse_workflow


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(description="Run and inspect local personal workflows.")
    cli.add_argument("--db", type=Path, default=Path(".harness/state.sqlite3"), help="SQLite path")
    commands = cli.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate", help="Check a workflow without creating state")
    validate.add_argument("definition", type=Path)
    start = commands.add_parser("start", help="Snapshot a workflow and create a run")
    start.add_argument("definition", type=Path)
    start.add_argument("--workspace", type=Path, default=Path.cwd(), help="Local command directory")
    resume = commands.add_parser("resume", help="Run until a gate or a terminal status")
    resume.add_argument("run_id")
    resume.add_argument("--allow-command", action="store_true", help="Run trusted local commands")
    resume.add_argument("--allow-codex", action="store_true", help="Send selected data to Codex")
    status = commands.add_parser("status", help="Inspect the current state")
    status.add_argument("run_id")
    status.add_argument("--outputs", action="store_true", help="Show persisted step outputs")
    events = commands.add_parser("events", help="Print structured events as JSON Lines")
    events.add_argument("run_id")
    for name in ("approve", "reject", "recover"):
        command = commands.add_parser(name, help=f"Record an operator decision ({name})")
        command.add_argument("run_id")
        command.add_argument("step_id")
        if name == "recover":
            command.add_argument("decision", choices=("retry", "fail"))
    return cli


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    adapters = built_in_adapters()
    try:
        if args.command in {"validate", "start"}:
            workflow = parse_workflow(args.definition.read_text(encoding="utf-8"), adapters)
            if args.command == "validate":
                print(json_text({"workflow": workflow.name, "steps": len(workflow.steps), "valid": True}))
                return 0
            workspace = args.workspace.resolve()
            if not workspace.is_dir():
                raise WorkflowError(f"Workspace must be an existing directory: {workspace}")
            store = Store(args.db, create=True)
            print(json_text({"run_id": store.create(workflow, workspace), "status": "ready"}))
            return 0

        store = Store(args.db)
        runner = Runner(store, adapters)
        if args.command == "events":
            for event in store.events(args.run_id):
                print(json_text(event))
            return 0
        if args.command == "status":
            print(json_text(runner.status(args.run_id, include_outputs=args.outputs)))
            return 0
        if args.command == "resume":
            enabled = {"mock"}
            if args.allow_command:
                enabled.add("command")
            if args.allow_codex:
                enabled.add("codex")
            report = runner.resume(args.run_id, enabled)
        elif args.command == "recover":
            report = runner.recover(args.run_id, args.step_id, retry=args.decision == "retry")
        else:
            report = runner.decide(args.run_id, args.step_id, approve=args.command == "approve")
        print(json_text(report))
        return {"failed": 1, "rejected": 1, "interrupted": 3}.get(report["status"], 0)
    except (WorkflowError, OSError, UnicodeError, sqlite3.Error) as error:
        print(f"harness: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("harness: interrupted. Inspect the run before resuming.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
