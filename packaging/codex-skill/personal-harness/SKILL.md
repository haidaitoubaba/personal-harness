---
name: personal-harness
description: Set up or use the packaged Personal Harness (通用 Harness) in Codex to run, approve, recover, back up, restore, or share local workflows, including when changing computers or AI accounts.
---

# Personal Harness

Use the packaged launcher to run and inspect workflows on the user's computer. When asked to set up from an extracted package, read its `START-HERE.txt` and run its `setup-codex` command.

## Locate the launcher

Use a package folder specified by the user, or read the absolute folder path from `runner-location.txt` beside this `SKILL.md`. If there is no binding, look for `Personal Harness` in Downloads. Use `harness` on macOS and `harness.cmd` on Windows. If the launcher is missing, locate the extracted folder and run `setup-codex` there to update the binding when the user asks to reconfigure.

Call the launcher by its full, quoted path. In PowerShell use `&` before a quoted launcher. For new runs, pass the absolute path to `.harness/state.sqlite3` in the active project or workspace using `--db` before the subcommand. For an existing run, use its original database.

## Work with a run

- For a demo, use the package's `examples/research-briefing.json`. For a real task, use the user's workflow definition or inspect project instructions and the relevant entry point before writing one. Run `validate DEFINITION` before `start DEFINITION --workspace TARGET`. Record the returned run ID.
- Run `resume RUN_ID` until it completes, fails, or reaches approval. Add `--allow-command` for the trusted commands the user wants run. Use `--allow-codex` when the user authorizes sending the selected inputs to a separately installed, signed-in Codex CLI.
- For approval, read `status RUN_ID --outputs` and show the result. Record the user's decision with `approve RUN_ID GATE_ID` or `reject RUN_ID GATE_ID`. Resume after approval if there are more steps.
- For an interrupted action, inspect `status` and `events` before asking whether to replay it. Use `recover RUN_ID STEP_ID retry` or `recover RUN_ID STEP_ID fail` for the user's decision.
- Report the run ID, absolute database path, and current status. Include the gate or step ID when it is needed to continue in another chat.

## Move or share workflows

- Use `--db PROJECT_DB backup PRIVATE_ZIP` to snapshot the correct project's state. Use repeated `--workflow DEFINITION` flags for any editable definitions the user wants included.
- Use `export-workflows SHARE_ZIP --workflow DEFINITION` for the files the user wants to share. Review the selected workflow for private prompts, inputs, paths, or hardcoded credentials before sharing.
- Run `inspect-backup ZIP` to check the bundle and report the run IDs, workspaces, and adapters. Use `restore ZIP NEW_FOLDER` and add `--map-workspace OLD_PATH NEW_PATH` when a saved workspace has moved. Use the reported database path for later run commands.
- Set up tools and account connections on the receiving machine as needed. To use a different model for an authorized Codex action, add `--codex-model MODEL` with `--allow-codex` on `resume`.
