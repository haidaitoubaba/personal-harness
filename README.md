# Personal Harness

A local Python runner for workflows that need retries, durable progress, and a human review step. A workflow is an ordered JSON file. The same runner can check software, draft a research briefing, or run another workflow built from its adapters.

## Run a briefing

Use Python 3.12 or later on macOS, Linux, or Windows to run from source. Run these shell examples from the project directory. The core runner uses the Python standard library.

```sh
python3 -m harness validate examples/research-briefing.json
RUN_ID=$(python3 -m harness start examples/research-briefing.json | python3 -c 'import json, sys; print(json.load(sys.stdin)["run_id"])')

python3 -m harness resume "$RUN_ID"
python3 -m harness status "$RUN_ID" --outputs
python3 -m harness approve "$RUN_ID" review_briefing
python3 -m harness events "$RUN_ID"
```

This example uses sample notes and deliberately retries the first collection step once. `resume` stops at `review_briefing`. The approval is the last step, so approving it completes the run. Use `reject "$RUN_ID" review_briefing` in place of `approve` to end it as rejected.

The default database is `.harness/state.sqlite3` in the working directory. Put `--db /path/to/state.sqlite3` before a subcommand to use a specific database. Pass the same database on subsequent calls. `start` snapshots the definition and resolves the workspace path. A later `resume` uses the saved definition, even if the original file has moved. Files in the command workspace can still change between runs.

## Run a software CI check

```sh
RUN_ID=$(python3 -m harness start examples/software-ci.json | python3 -c 'import json, sys; print(json.load(sys.stdin)["run_id"])')
python3 -m harness resume "$RUN_ID" --allow-command
python3 -m harness status "$RUN_ID" --outputs
python3 -m harness approve "$RUN_ID" review_results
python3 -m harness resume "$RUN_ID"
```

The command runs the Harness's test suite in the current workspace. Its exit code determines whether the workflow can reach the review. For another project, use a trusted definition that names its test command and set `start --workspace /path/to/project`.

## Try the Codex adapter

Use an installed, signed-in Codex CLI with support for `codex exec`, `--ignore-user-config`, and `--output-last-message`. The following command sequence sends the example prompt and synthetic notes to Codex when you explicitly allow the adapter.

```sh
RUN_ID=$(python3 -m harness start examples/research-briefing-codex.json | python3 -c 'import json, sys; print(json.load(sys.stdin)["run_id"])')
python3 -m harness resume "$RUN_ID" --allow-codex
python3 -m harness status "$RUN_ID" --outputs
python3 -m harness approve "$RUN_ID" review_briefing
```

The adapter runs in a fresh temporary directory with the CLI's read-only sandbox and ephemeral mode. It ignores user configuration and disables apps, plugins, hooks, browser use, computer use, and web search. The final reply must be a single JSON object. The workflow checks its fields and source IDs. Human review covers the accuracy of the summary.

## Define a workflow

Every definition has `version` set to `1`, a `name`, and a nonempty `steps` array. Step IDs begin with a lowercase letter and may contain lowercase letters, digits, underscores, and hyphens.

- An `approval` step has an `id`, `type`, and reviewer `message`. The runner waits for the exact run and step ID. An approval records the decision. Use `resume` if there are downstream steps.
- An `action` step has an `id`, `type`, `adapter`, and adapter-specific `config`. Optional `from_steps` selects outputs from earlier actions. `max_attempts` defaults to 1 and accepts integers from 1 to 5. `timeout_seconds` defaults to 60 and accepts values from 0.1 to 1800.
- Optional `validate` checks top-level output fields. `types` requires a field and one JSON type (`array`, `boolean`, `integer`, `null`, `number`, `object`, or `string`). `equals` requires an exact JSON value. `nonempty` lists strings, arrays, or objects that must contain data. These checks run before an output is stored or supplied to another step.

| Adapter | Configuration | Behavior |
| --- | --- | --- |
| `mock` | `outcomes` array containing `{"output": {...}}` or `{"fail": true}` | Chooses by attempt number and repeats the last entry when needed. Works offline. |
| `command` | `argv` array of strings | Starts the named executable directly with local user permissions. Selected outputs arrive as a JSON object on stdin. A zero exit returns `exit_code` and `stdout`. Requires `--allow-command` for each invocation of `resume` that executes it. |
| `codex` | `prompt` string | Sends the prompt and selected outputs to Codex and returns the parsed final JSON object. Requires `--allow-codex` for each invocation of `resume` that executes it. |

Python callers can add an adapter by passing a registry to `parse_workflow` and `Runner`. An adapter validates its config with `check` and returns a JSON object from `execute`. Its context provides the run ID, step ID, workspace, attempt number, timeout, and selected inputs. Custom adapters enforce their own timeout and use `ActionError` for expected failures.

## Review state or recover a run

`status --outputs` shows persisted outputs. `events` emits JSON Lines with a sequence number, UTC timestamp, event name, run ID, and step and attempt when applicable. Failure events include a structured reason, retry decision, and process exit code where available. Raw stdout, stderr, and prompts stay out of the event stream. The SQLite database contains definitions, including prompts, and successful outputs. Use trusted definitions and data suitable for local persistence and, when enabled, the configured Codex service.

Each run allows one advancing process at a time. An action failure retries within its attempt budget. A timeout stops the built-in adapter's subprocess group. Keep retryable steps safe to repeat. If the runner dies mid-attempt, the next `resume` marks the action as interrupted and stops. Check whether the earlier process is still active and whether it produced any side effects before deciding how to proceed. The interrupted attempt still counts against the limit.

```sh
python3 -m harness recover "$RUN_ID" STEP_ID retry
python3 -m harness resume "$RUN_ID"
```

Use `recover "$RUN_ID" STEP_ID fail` to close the run instead. Include the relevant adapter flag if resuming a command or Codex action. From `resume`, exit codes are 0 for success or a pending approval, 1 for a failed or rejected run, 2 for invalid input or an unavailable prerequisite, 3 when interrupted recovery is required, and 130 for a keyboard interrupt.

## Verify locally

```sh
python3 -m unittest discover -s tests -v
for f in examples/*.json; do python3 -m harness validate "$f" || exit 1; done
```

The tests use temporary SQLite databases and a fake Codex executable. See [PLAN.md](PLAN.md) for the verification record.

## Build portable packages for colleagues

The macOS and Windows bundles include a Python runtime, example workflows, the full command-line interface, and a double-click offline demo. Extract the complete ZIP and open the platform's **Start Harness** launcher. See [START-HERE.txt](packaging/START-HERE.txt) for instructions included in each bundle.

On a Mac, run the builder with Python 3.14.10 (universal2) and PyInstaller 6.22.3 installed in its build environment.

```sh
python3.14 packaging/build.py macos --output-dir dist
```

To build the Windows x64 ZIP on any platform, download the official Python 3.14.10 Windows x64 embeddable archive. The builder checks its pinned SHA-256 before extracting it.

```sh
python3 packaging/build.py windows --output-dir dist --python-embed python-3.14.10-embed-amd64.zip
```

The packaged offline demo runs without Codex. Codex workflows require a separate Codex CLI installation and sign-in. Application data and workflow outputs are stored locally in SQLite.

### Repeat the builds and checks with Personal Harness

From the repository root on macOS, use an environment where `python3.14` is the universal2 Python 3.14.10 with PyInstaller 6.22.3 installed. Place the official `python-3.14.10-embed-amd64.zip` in `dist/`. Set `HARNESS_BUILD_DIR` to use a different directory for both the input ZIP and build outputs.

```sh
python3.14 -m harness validate packaging/package-check.json
RUN_ID=$(python3.14 -m harness start packaging/package-check.json --workspace "$PWD" | python3.14 -c 'import json, sys; print(json.load(sys.stdin)["run_id"])')
python3.14 -m harness resume "$RUN_ID" --allow-command
python3.14 -m harness status "$RUN_ID" --outputs
```

Each successful build writes both ZIPs to a fresh `personal-harness-*` directory. The run records source tests, SHA-256 digests, the packaged macOS demo and CLI checks, and Windows runtime and ZIP verification. Run `Start Harness.cmd` from an extracted ZIP on native Windows to complete the Windows execution check.
