# Personal Harness

A local Python runner for workflows that need retries, durable progress, and a human review step. A workflow is an ordered JSON file. The same runner can check software, draft a research briefing, or run another workflow built from its adapters.

## Use the packaged release

Each v0.1.2 ZIP includes Personal Harness, Python 3.14.10, the Codex Skill, and tools to back up and restore runs or share workflow definitions. [v0.1.2 release notes](https://github.com/haidaitoubaba/personal-harness/releases/tag/v0.1.2)

### Download and set up in Codex

1. Download one ZIP for your computer. [macOS for Apple silicon and Intel](https://github.com/haidaitoubaba/personal-harness/releases/download/v0.1.2/Personal-Harness-macOS.zip) or [Windows x64](https://github.com/haidaitoubaba/personal-harness/releases/download/v0.1.2/Personal-Harness-Windows-x64.zip).
2. Extract the whole ZIP and open the resulting `Personal Harness` folder in Codex.
3. Say:

   > Set up Personal Harness from this folder using START-HERE.txt.

Codex uses the included setup command to install the Skill and remember the path to the extracted program. If Codex asks for permission to write to its Skills directory, approve that installation. Keep the extracted folder together and repeat setup if you move it.

### Use Personal Harness in Codex

In a new Codex chat, say:

> Use Personal Harness to run the offline research briefing and wait for my approval.

To run a real project check, open that project in Codex and say:

> Use Personal Harness to run this project's tests and wait for my approval.

The offline demo uses sample notes. Codex workflows that generate a briefing use a separately installed, signed-in Codex CLI.

### Run directly from the desktop or terminal

For the offline demo, double-click `Start Harness.command` on macOS or `Start Harness.cmd` on Windows. Press Enter to start. At review, type `y` to approve, `n` to reject, or press Enter to leave it pending. Results are saved in `.harness/state.sqlite3` inside the extracted folder.

To use a terminal, open it inside the extracted folder. Replace `RUN_ID` with the value printed by `start`.

**macOS**

```sh
./harness validate examples/research-briefing.json
./harness start examples/research-briefing.json
./harness resume RUN_ID
./harness status RUN_ID --outputs
./harness approve RUN_ID review_briefing
```

**Windows PowerShell**

```powershell
.\harness.cmd validate examples/research-briefing.json
.\harness.cmd start examples/research-briefing.json
.\harness.cmd resume RUN_ID
.\harness.cmd status RUN_ID --outputs
.\harness.cmd approve RUN_ID review_briefing
```

Use `reject RUN_ID review_briefing` to reject the draft. Run later commands from the same folder to use the same run database.

For a project check, copy `examples/software-ci.json`, set `config.argv` to the test command, start the workflow with `--workspace` set to the project, and use `resume RUN_ID --allow-command`. After approval, resume again to finish the example's last step. For a Codex briefing, start with `examples/research-briefing-codex.json` and use `resume RUN_ID --allow-codex` to send the selected inputs to Codex. See `START-HERE.txt` in the extracted folder for launcher and database options.

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

## Back up, restore, and share workflows

The v0.1.2 packages support these commands. Replace `python3 -m harness` with `./harness` on macOS or `.\harness.cmd` in Windows PowerShell when running from the extracted folder.

### Move your Harness to another computer or account

```sh
python3 -m harness --db /old/project/.harness/state.sqlite3 backup personal.zip --workflow examples/research-briefing.json
python3 -m harness inspect-backup personal.zip
python3 -m harness restore personal.zip migrated --map-workspace /old/project /new/project
python3 -m harness --db migrated/.harness/state.sqlite3 status RUN_ID --outputs
```

`backup` takes a consistent snapshot of one SQLite database, including run IDs, workflow snapshots, outputs, attempts, approvals, and events. Repeat `--workflow` to also keep editable copies of selected definitions. Keep this ZIP private. It is unencrypted and can contain sensitive content saved by your workflows.

Use `inspect-backup` to verify the bundle's checksums and view the run IDs, saved workspaces, and required adapters. Restore into a new directory under an existing parent. The original database stays in place. Use the restored database path for subsequent `status`, `approve`, `recover`, and `resume` commands. If a path changed, pass the exact old path from the preview and an existing destination workspace. Repeat `--map-workspace` for additional paths. The restore reports saved paths that are still unavailable. Stop advancing the original run before switching to the restored copy.

Install or sign in to the required tools on the destination. Run `setup-codex` there if you use the bundled Codex integration. The bundle covers Harness state and selected JSON files. Transfer project repositories and inputs separately, and reauthorize account connections on the new platform. ChatGPT chats, platform settings, credentials, installed plugins, and the contents of the saved workspaces are outside the bundle.

You can invoke the Harness CLI from a different AI client with terminal access. Changing the AI provider that **executes a step** requires an appropriate adapter or an updated workflow for a new run. The preview lists adapter IDs so you can check compatibility before continuing. To use a different model for pending Codex steps, run `resume RUN_ID --allow-codex --codex-model MODEL` against the restored database. This setting applies to the current invocation and preserves the run's saved workflow and completed outputs.

### Send a workflow to a colleague

```sh
python3 -m harness export-workflows team-workflows.zip --workflow examples/research-briefing.json
python3 -m harness inspect-backup team-workflows.zip
python3 -m harness restore team-workflows.zip shared
```

The export includes only the selected workflow definitions and their manifest. Review the JSON for embedded inputs, prompts, paths, and credentials before sharing. The recipient can review the adapter list, restore the files under `shared/workflows/`, and use `validate` and `start` with their own workspace. Restoring a bundle verifies files and does not execute workflow steps.

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

## Verify published packages

For the next packaged release, create a new `release/<version>` branch from `release/v0.1.1`, the current packaging baseline. Tag the prepared release commit on the new branch. For later versions, start from the most recent release branch with the packaging files and keep `.github/workflows/verify-published-packages.yml` in the tagged commit.

Attach `Personal-Harness-macOS.zip` and `Personal-Harness-Windows-x64.zip` to the GitHub Release, then publish it. Publishing a release or prerelease starts the checks for that tag. The Windows x64 and Intel Mac jobs download the published ZIPs, verify their SHA-256 digests, run the offline demo, and check that CLI approval and rejection results are saved and can be read back.

### Recheck a published version

1. In **Actions**, open [Verify published packages](https://github.com/haidaitoubaba/personal-harness/actions/workflows/verify-published-packages.yml).
2. Select **Run workflow** and leave **Use workflow from** set to `main`.
3. Enter the exact published tag in **Tag of an existing published release**, for example `v0.1.0-alpha.2`, and select **Run workflow**.
4. Open the new run and check the results for **Windows x64** and **Intel Mac**.
