import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from contextlib import closing
from pathlib import Path

from harness.adapters import built_in_adapters
from harness.backup import MANIFEST, STATE
from harness.store import Store
from harness.workflow import ActionError, WorkflowError, parse_workflow, validate_output

ROOT = Path(__file__).resolve().parents[1]
RESEARCH = ROOT / "examples/research-briefing.json"
CODEX = ROOT / "examples/research-briefing-codex.json"


def mock_action(**overrides):
    action = {
        "id": "demo", "type": "action", "adapter": "mock",
        "config": {"outcomes": [{"output": {"ok": True}}]},
    }
    return action | overrides


class WorkflowTests(unittest.TestCase):
    def test_definition_rejects_structural_errors(self):
        approval = {"id": "review", "type": "approval", "message": "Check it."}
        invalid = [
            [mock_action(adapter=[])],
            [mock_action(config={"outcomes": [{"fail": "yes"}]})],
            [mock_action(max_attempts=True)],
            [mock_action(max_attempts=6)],
            [mock_action(timeout_seconds=0)],
            [mock_action(timeout_seconds=1801)],
            [mock_action(from_steps=["later"]), mock_action(id="later")],
            [mock_action(), mock_action()],
            [approval, mock_action(from_steps=["review"])],
            [mock_action(validate={"types": {"ok": "boolean"}, "nonempty": ["ok"]})],
            [mock_action(extra_field="typo")],
        ]
        for steps in invalid:
            with self.subTest(steps=steps), self.assertRaises(WorkflowError):
                parse_workflow(
                    json.dumps({"version": 1, "name": "Invalid", "steps": steps}),
                    built_in_adapters(),
                )

    def test_duplicate_keys_and_nonfinite_numbers_rejected(self):
        for value in ('{"version": 1, "version": 1}', '{"version": NaN}', '{"version": 1e999}'):
            with self.subTest(value=value), self.assertRaises(WorkflowError):
                parse_workflow(value, built_in_adapters())

    def test_output_checks_use_strict_json_types_and_equality(self):
        with self.assertRaisesRegex(ActionError, "output_equality_check_failed"):
            validate_output({"value": True}, {"equals": {"value": 1}})
        with self.assertRaisesRegex(ActionError, "output_type_check_failed"):
            validate_output({"value": True}, {"types": {"value": "integer"}})
        with self.assertRaisesRegex(ActionError, "output_nonempty_check_failed"):
            validate_output({"value": "  "}, {"nonempty": ["value"]})


class HarnessCLITests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.db = self.folder / "run state.sqlite3"

    def cli(self, *args, code=0, db=None):
        process = subprocess.run(
            [sys.executable, "-m", "harness", "--db", str(db or self.db), *map(str, args)],
            cwd=ROOT, capture_output=True, text=True, timeout=10, check=False,
        )
        self.assertEqual(process.returncode, code, process.stderr + process.stdout)
        if args[0] == "events":
            return [json.loads(line) for line in process.stdout.splitlines()]
        return json.loads(process.stdout) if process.stdout else process.stderr

    def definition(self, *steps):
        path = self.folder / "workflow.json"
        path.write_text(json.dumps({"version": 1, "name": "Test", "steps": steps}), encoding="utf-8")
        return path

    def begin(self, definition):
        return self.cli("start", definition, "--workspace", self.folder)["run_id"]

    def test_approval_resume_uses_snapshot_and_runs_completed_steps_once(self):
        path = self.folder / "briefing.json"
        path.write_text(RESEARCH.read_text(encoding="utf-8"), encoding="utf-8")
        run = self.begin(path)
        path.unlink()
        report = self.cli("resume", run)
        self.assertEqual(report["status"], "waiting_for_approval")
        self.assertIn("persisted draft", report["approval_message"])
        self.assertEqual(report["steps"][0]["attempts"], 2)
        self.assertNotIn("output", report["steps"][1])
        outputs = self.cli("status", run, "--outputs")
        self.assertEqual(outputs["steps"][1]["output"]["sources"], ["sample-1"])
        self.assertEqual(self.cli("resume", run)["status"], "waiting_for_approval")
        self.cli("approve", run, "draft_briefing", code=2)
        self.assertEqual(self.cli("approve", run, "review_briefing")["status"], "succeeded")
        self.assertEqual(self.cli("resume", run)["status"], "succeeded")
        self.cli("approve", run, "review_briefing", code=2)
        events = self.cli("events", run)
        self.assertEqual(
            [event["kind"] for event in events],
            [
                "run_created", "attempt_started", "attempt_failed", "attempt_started",
                "attempt_succeeded", "attempt_started", "attempt_succeeded",
                "approval_requested", "approval_granted", "run_succeeded",
            ],
        )
        self.assertNotIn("Sample metadata", json.dumps(events))
        self.assertEqual(events[2]["details"], {"reason": "mock_failure", "will_retry": True})

    def test_rejection_prevents_downstream_execution(self):
        path = self.definition(
            {"id": "review", "type": "approval", "message": "Approve?"},
            mock_action(),
        )
        run = self.begin(path)
        self.cli("approve", run, "review", code=2)
        self.cli("resume", run)
        report = self.cli("reject", run, "review", code=1)
        self.assertEqual(report["status"], "rejected")
        self.assertEqual(self.cli("resume", run, code=1)["steps"][1]["attempts"], 0)

    def test_validation_and_retry_exhaustion_are_persisted(self):
        path = self.definition(
            mock_action(max_attempts=3, validate={"types": {"ok": "string"}}),
        )
        run = self.begin(path)
        report = self.cli("resume", run, code=1)
        self.assertEqual(report["steps"][0]["attempts"], 3)
        self.assertEqual(report["steps"][0]["reason"], "output_type_check_failed")
        self.assertEqual(self.cli("resume", run, code=1), report)
        events = self.cli("events", run)
        failures = [item["details"]["will_retry"] for item in events if item["kind"] == "attempt_failed"]
        self.assertEqual(failures, [True, True, False])

    def test_concurrent_resume_is_refused_and_untouched(self):
        run = self.begin(self.definition(mock_action()))
        with Store(self.db).run_lock(run):
            self.assertIn("already being advanced", self.cli("resume", run, code=2))
            self.assertEqual(self.cli("status", run)["steps"][0]["attempts"], 0)
        self.assertEqual(self.cli("resume", run)["status"], "succeeded")

    def interrupted_run(self, max_attempts):
        run = self.begin(self.definition(
            mock_action(id="finished"),
            mock_action(
                max_attempts=max_attempts,
                config={"outcomes": [{"output": {"crash": True}}, {"output": {"ok": True}}]},
            ),
        ))
        script = """
import os
import sys
from pathlib import Path
from harness.adapters import MockAdapter
from harness.runner import Runner
from harness.store import Store

class Interrupted(MockAdapter):
    def execute(self, config, context):
        if context.step_id == "demo":
            os._exit(77)
        return super().execute(config, context)

Runner(Store(Path(sys.argv[1])), {"mock": Interrupted()}).resume(sys.argv[2], {"mock"})
"""
        process = subprocess.run(
            [sys.executable, "-c", script, str(self.db), run], cwd=ROOT, timeout=10, check=False,
        )
        self.assertEqual(process.returncode, 77)
        self.assertEqual(self.cli("status", run)["status"], "running")
        return run

    def test_crash_requires_explicit_recovery_and_preserves_completed_steps(self):
        run = self.interrupted_run(max_attempts=2)
        self.assertEqual(self.cli("resume", run, code=3)["status"], "interrupted")
        self.assertEqual(self.cli("resume", run, code=3)["steps"][1]["attempts"], 1)
        self.cli("recover", run, "finished", "retry", code=2)
        self.assertEqual(self.cli("recover", run, "demo", "retry")["status"], "ready")
        self.assertEqual(self.cli("resume", run)["status"], "succeeded")
        report = self.cli("status", run, "--outputs")
        self.assertEqual(report["steps"][0]["attempts"], 1)
        self.assertEqual(report["steps"][1]["attempts"], 2)
        self.assertEqual(report["steps"][1]["output"], {"ok": True})

    def test_crash_consumes_attempt_even_at_limit(self):
        run = self.interrupted_run(max_attempts=1)
        self.assertIn("No attempts remain", self.cli("recover", run, "demo", "retry", code=2))
        report = self.cli("recover", run, "demo", "fail", code=1)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["steps"][1]["attempts"], 1)

    def test_command_opt_in_and_nonzero_exit_status(self):
        run = self.begin(self.definition(mock_action(
            adapter="command",
            config={"argv": [sys.executable, "-c", "raise SystemExit(14)"]},
        )))
        self.assertIn("--allow-command", self.cli("resume", run, code=2))
        self.assertEqual(self.cli("status", run)["steps"][0]["attempts"], 0)
        report = self.cli("resume", run, "--allow-command", code=1)
        self.assertEqual(report["steps"][0]["reason"], "process_nonzero_exit")
        failure = next(event for event in self.cli("events", run) if event["kind"] == "attempt_failed")
        self.assertEqual(failure["details"]["exit_code"], 14)

    def test_timeout_retries_and_kills_child_processes(self):
        launched = self.folder / "launched"
        marker = self.folder / "escaped"
        child = (
            "from pathlib import Path; import sys, time; Path(sys.argv[1]).touch(); "
            "time.sleep(2); Path(sys.argv[2]).touch()"
        )
        parent = "import subprocess, sys, time; subprocess.Popen(sys.argv[1:]); time.sleep(10)"
        run = self.begin(self.definition(mock_action(
            adapter="command",
            config={"argv": [
                sys.executable, "-c", parent, sys.executable, "-c", child, str(launched), str(marker),
            ]},
            timeout_seconds=0.9,
            max_attempts=2,
        )))
        started = time.monotonic()
        report = self.cli("resume", run, "--allow-command", code=1)
        self.assertLess(time.monotonic() - started, 7)
        self.assertEqual(report["steps"][0]["attempts"], 2)
        self.assertEqual(report["steps"][0]["reason"], "timeout")
        self.assertTrue(launched.exists())
        time.sleep(2)
        self.assertFalse(marker.exists())

    def test_command_limits_captured_output(self):
        run = self.begin(self.definition(mock_action(
            adapter="command",
            config={"argv": [sys.executable, "-c", "import sys; sys.stdout.write('a' * 300000)"]},
        )))
        report = self.cli("resume", run, "--allow-command", code=1)
        self.assertEqual(report["steps"][0]["reason"], "process_output_too_large")

    def test_command_runs_in_work_directory_and_receives_selected_inputs(self):
        script = "import json, sys; from pathlib import Path; print(Path.cwd().name, json.load(sys.stdin))"
        run = self.begin(self.definition(
            mock_action(id="source"),
            mock_action(id="unselected"),
            mock_action(
                adapter="command", from_steps=["source"],
                config={"argv": [sys.executable, "-c", script]},
                validate={"nonempty": ["stdout"], "equals": {"exit_code": 0}},
            ),
        ))
        self.assertEqual(self.cli("resume", run, "--allow-command")["status"], "succeeded")
        output = self.cli("status", run, "--outputs")["steps"][2]["output"]["stdout"]
        self.assertEqual(output.strip(), f"{self.folder.name} {{'source': {{'ok': True}}}}")

    def test_codex_subprocess_contract_with_local_fake(self):
        fake = self.folder / "fake_codex.py"
        fake.write_text(
            """
import json
import sys
from pathlib import Path

args = sys.argv[1:]
assert args[0] == "exec"
assert "--model=test-model" in args
for flag in ("--ignore-user-config", "--ephemeral", "--skip-git-repo-check"):
    assert flag in args
assert args[args.index("--sandbox") + 1] == "read-only"
assert 'approval_policy="never"' in args
assert 'web_search="disabled"' in args
assert args[-1] == "-"
assert not list(Path.cwd().iterdir())
text = sys.stdin.read()
assert "sample-1" in text and "Inputs (JSON)" in text
target = Path(args[args.index("--output-last-message") + 1])
assert target.parent == Path.cwd().resolve()
target.write_text(json.dumps({
    "summary": "Summary from fake Codex",
    "next_actions": ["Prepare discussion questions"],
    "sources": ["sample-1"],
}))
""",
            encoding="utf-8",
        )
        run = self.begin(CODEX)
        self.assertIn("--allow-codex", self.cli("resume", run, code=2))
        self.assertIn("--allow-codex", self.cli("resume", run, "--codex-model", "test-model", code=2))
        self.assertEqual(self.cli("status", run)["steps"][1]["attempts"], 0)
        script = """
import sys
from harness import __main__ as cli
from harness.adapters import CodexAdapter, built_in_adapters

cli.built_in_adapters = lambda *, codex_model=None: built_in_adapters() | {
    "codex": CodexAdapter([sys.executable, sys.argv[1]], model=codex_model)
}
raise SystemExit(cli.main(sys.argv[2:]))
"""
        result = subprocess.run(
            [sys.executable, "-c", script, str(fake), "--db", str(self.db),
             "resume", run, "--allow-codex", "--codex-model", "test-model"],
            cwd=ROOT, capture_output=True, text=True, timeout=10, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        report = json.loads(result.stdout)
        self.assertEqual(report["status"], "waiting_for_approval")
        draft = self.cli("status", run, "--outputs")["steps"][1]["output"]
        self.assertEqual(draft["summary"], "Summary from fake Codex")
        self.assertNotIn("Summary from fake Codex", json.dumps(self.cli("events", run)))
        self.assertEqual(self.cli("approve", run, "review_briefing")["status"], "succeeded")

    def test_validate_leaves_state_untouched(self):
        self.assertTrue(self.cli("validate", RESEARCH)["valid"])
        self.assertFalse(self.db.exists())
        path = self.definition(mock_action(max_attempts=0))
        self.cli("start", path, code=2)
        self.assertFalse(self.db.exists())

    def test_backup_restores_committed_wal_state_and_run_continues_after_move(self):
        previous = self.folder / "old workspace"
        previous.mkdir()
        replacement = self.folder / "new workspace"
        replacement.mkdir()
        definition = self.definition(
            mock_action(config={"outcomes": [{"output": {"draft": "private research"}}]}),
            {"id": "review", "type": "approval", "message": "Review the draft."},
            mock_action(
                id="where", adapter="command", from_steps=["demo"],
                config={"argv": [sys.executable, "-c", "from pathlib import Path; print(Path.cwd().name)"]},
            ),
        )
        # Leave a connection open so the committed data stays in the WAL.
        with closing(sqlite3.connect(self.db, isolation_level=None)) as wal:
            self.assertEqual(wal.execute("PRAGMA journal_mode=WAL").fetchone(), ("wal",))
            wal.execute("BEGIN")
            wal.execute("SELECT name FROM sqlite_schema").fetchall()
            run = self.cli("start", definition, "--workspace", previous)["run_id"]
            self.assertEqual(self.cli("resume", run)["status"], "waiting_for_approval")
            initial = self.cli("status", run, "--outputs")
            events = self.cli("events", run)
            archive = self.folder / "private.zip"
            created = self.cli("backup", archive, "--workflow", definition)

        preview = self.cli("inspect-backup", archive)
        self.assertTrue(preview["includes_state"])
        self.assertEqual(preview["runs"][0]["run_id"], run)
        self.assertEqual(preview["required_adapters"], ["command", "mock"])
        previous.rmdir()
        unmapped = self.cli("restore", archive, self.folder / "unmapped")
        self.assertEqual(unmapped["unavailable_workspaces"], [str(previous.resolve())])
        before = self.folder / "cannot restore"
        self.assertIn("No run", self.cli("restore", archive, before, "--map-workspace", "typo", replacement, code=2))
        self.assertFalse(before.exists())

        target = self.folder / "restored"
        restored = self.cli("restore", archive, target, "--map-workspace", previous.resolve(), replacement)
        db = Path(restored["database"])
        self.assertEqual(restored["unavailable_workspaces"], [])
        self.assertEqual(db, target.resolve() / STATE)
        self.assertEqual((target / created["workflows"][0]["path"]).read_bytes(), definition.read_bytes())
        self.assertEqual(self.cli("status", run, "--outputs", db=db), initial)
        self.assertEqual(self.cli("events", run, db=db), events)
        self.assertEqual(self.cli("resume", run, db=db)["status"], "waiting_for_approval")
        self.cli("approve", run, "review", db=db)
        self.assertIn("--allow-command", self.cli("resume", run, db=db, code=2))
        self.assertEqual(self.cli("resume", run, "--allow-command", db=db)["status"], "succeeded")
        resumed = self.cli("status", run, "--outputs", db=db)
        self.assertEqual(resumed["steps"][0]["attempts"], 1)
        self.assertEqual(resumed["steps"][2]["output"]["stdout"].strip(), replacement.name)
        self.assertEqual(self.cli("status", run, "--outputs"), initial)

    def test_workflow_export_excludes_state_and_lists_custom_adapter_dependencies(self):
        run = self.begin(self.definition(mock_action(config={"outcomes": [{"output": {"key": "private output"}}]})))
        self.cli("resume", run)
        custom = self.folder / "other workflow.json"
        custom.write_text(json.dumps({
            "version": 1, "name": "Other platform", "steps": [mock_action(adapter="other-platform", config={})],
        }), encoding="utf-8")
        archive = self.folder / "share.zip"
        report = self.cli("export-workflows", archive, "--workflow", RESEARCH, "--workflow", custom)
        self.assertEqual(report["runs"], [])
        self.assertEqual(report["required_adapters"], ["mock", "other-platform"])
        with zipfile.ZipFile(archive) as bundle:
            self.assertEqual(set(bundle.namelist()), {MANIFEST, *(item["path"] for item in report["workflows"])})
            self.assertTrue(all(b"private output" not in bundle.read(name) for name in bundle.namelist()))
        target = self.folder / "received"
        restored = self.cli("restore", archive, target)
        self.assertIsNone(restored["database"])
        definition = target / restored["workflows"][0]["path"]
        db = target / STATE
        self.assertFalse(db.exists())
        new_run = self.cli("start", definition, "--workspace", target, db=db)["run_id"]
        self.assertNotEqual(new_run, run)
        self.assertEqual(self.cli("resume", new_run, db=db)["status"], "waiting_for_approval")

    def test_restore_refuses_corrupt_unsafe_and_existing_destinations(self):
        archive = self.folder / "archive.zip"
        report = self.cli("export-workflows", archive, "--workflow", RESEARCH)
        original = archive.read_bytes()
        self.assertIn("already exists", self.cli("export-workflows", archive, "--workflow", RESEARCH, code=2))
        self.assertEqual(archive.read_bytes(), original)
        target = self.folder / "existing"
        target.mkdir()
        self.assertIn("already exists", self.cli("restore", archive, target, code=2))
        self.assertEqual(list(target.iterdir()), [])

        with zipfile.ZipFile(archive) as bundle:
            manifest = json.loads(bundle.read(MANIFEST))
        name = report["workflows"][0]["path"]
        target = self.folder / "invalid"
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr(MANIFEST, json.dumps(manifest))
            bundle.writestr(name, RESEARCH.read_bytes() + b" ")
        self.assertIn("checksum failed", self.cli("restore", archive, target, code=2))
        self.assertFalse(target.exists())

        payload = RESEARCH.read_bytes()
        manifest["files"] = {"../escaped.json": hashlib.sha256(payload).hexdigest()}
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr(MANIFEST, json.dumps(manifest))
            bundle.writestr("../escaped.json", payload)
        self.assertIn("invalid file path", self.cli("restore", archive, target, code=2))
        self.assertFalse(target.exists())
        self.assertFalse((self.folder / "escaped.json").exists())


if __name__ == "__main__":
    unittest.main()
