# Personal Harness MVP plan

Build a local Python runner that can run the same workflow mechanics for software checks and research briefings. Start with ordered JSON steps and one local operator.

- [x] Inspect the local workspace and create an isolated project directory.
- [x] Validate workflow definitions before creating a run. Snapshot the definition and workspace in SQLite.
- [x] Persist step and attempt transitions, approval decisions, outputs, and structured events. Serialize execution per run. Require a human decision before replaying an interrupted action.
- [x] Add interchangeable mock, local command, and read-only Codex CLI adapters. Apply deterministic output checks and cap attempts and subprocess duration.
- [x] Ship standalone examples for local software CI and a research briefing, plus a Codex briefing variant.
- [x] Verify both offline examples, approval and rejection, restart and crash recovery, concurrent execution, retries, timeouts, and the Codex CLI subprocess contract.
- [x] Document usage and verification for local development.

The first version uses the Python standard library on macOS and Linux. Run with the CLI on demand. A scheduler, parallel steps, shared users, publication, and hosted deployment are outside this MVP.

## Verification record

- All 14 tests passed on Python 3.12 on macOS. The suite exercised an abrupt process exit, persisted recovery, and a subprocess timeout with a child process.
- Both offline examples reached approval and completed successfully. All three JSON workflow definitions passed validation.
- The Codex adapter passed an end-to-end check with a local fake executable. The installed CLI accepted the adapter's flags in a help invocation. Live Codex inference remains unverified.
- The code is prepared for GitHub-only source hosting. The verification commands in the README run locally.
