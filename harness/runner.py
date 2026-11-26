"""Execute and resume runs using the persisted definition."""

import json
from collections.abc import Mapping
from pathlib import Path

from .store import Store
from .workflow import (
    Action,
    ActionError,
    Adapter,
    Approval,
    Context,
    JsonObject,
    WorkflowError,
    parse_workflow,
    validate_output,
)

class Runner:
    def __init__(self, store: Store, adapters: Mapping[str, Adapter]):
        self.store = store
        self.adapters = adapters

    def status(self, run_id: str, *, include_outputs: bool = False) -> JsonObject:
        report = self.store.status(run_id, include_outputs=include_outputs)
        if report["status"] == "waiting_for_approval":
            workflow = parse_workflow(self.store.run(run_id)["definition"], self.adapters)
            step = next(step for step in workflow.steps if step.id == report["current_step"])
            assert isinstance(step, Approval)
            report["approval_message"] = step.message
        return report

    def resume(self, run_id: str, enabled: set[str]) -> JsonObject:
        with self.store.run_lock(run_id):
            report = self.store.status(run_id, include_outputs=True)
            if report["status"] == "running":
                self._mark_interrupted(run_id, report["current_step"])
                return self.status(run_id)
            if report["status"] != "ready":
                return self.status(run_id)

            run = self.store.run(run_id)
            workflow = parse_workflow(run["definition"], self.adapters)
            workspace = Path(run["workspace"])
            outputs: dict[str, JsonObject] = {}
            for step, saved in zip(workflow.steps, report["steps"], strict=True):
                if saved["status"] == "succeeded":
                    if isinstance(step, Action):
                        outputs[step.id] = saved["output"]
                    continue
                if isinstance(step, Approval):
                    self.store.transition(
                        run_id, step.id, "pending", "waiting_for_approval", "approval_requested",
                    )
                    break
                if step.adapter not in enabled:
                    raise WorkflowError(
                        f"Adapter {step.adapter!r} is disabled for this invocation. "
                        f"Run {run_id} is ready. If authorized, resume with --allow-{step.adapter}."
                    )
                if step.adapter == "command" and not workspace.is_dir():
                    raise WorkflowError(f"Run workspace is unavailable: {workspace}")
                inputs = {step_id: outputs[step_id] for step_id in step.from_steps}
                output = self._execute(run_id, step, workspace, saved["attempts"], inputs)
                if output is None:
                    break
                outputs[step.id] = output
            return self.status(run_id)

    def _execute(
        self,
        run_id: str,
        step: Action,
        workspace: Path,
        attempts: int,
        inputs: dict[str, JsonObject],
    ) -> JsonObject | None:
        for attempt in range(attempts + 1, step.max_attempts + 1):
            self.store.transition(
                run_id, step.id, "pending", "running", "attempt_started",
                start_attempt=True, details={"adapter": step.adapter, "timeout": step.timeout_seconds},
            )
            context = Context(run_id, step.id, workspace, attempt, step.timeout_seconds, inputs)
            try:
                output = self.adapters[step.adapter].execute(step.config, context)
                serialized = validate_output(output, step.checks)
            except ActionError as error:
                retry = error.retryable and attempt < step.max_attempts
                details = {"will_retry": retry}
                if error.exit_code is not None:
                    details["exit_code"] = error.exit_code
                self.store.transition(
                    run_id, step.id, "running", "pending" if retry else "failed",
                    "attempt_failed", reason=error.reason, details=details,
                )
                if not retry:
                    return None
            else:
                self.store.transition(
                    run_id, step.id, "running", "succeeded", "attempt_succeeded",
                    output=serialized,
                )
                return json.loads(serialized)
        raise WorkflowError(f"Step {step.id} has exhausted its attempts")

    def decide(self, run_id: str, step_id: str, *, approve: bool) -> JsonObject:
        with self.store.run_lock(run_id):
            report = self.store.status(run_id)
            if report["status"] != "waiting_for_approval" or report["current_step"] != step_id:
                raise WorkflowError(f"Run {run_id} is not waiting for approval of {step_id}")
            self.store.transition(
                run_id, step_id, "waiting_for_approval", "succeeded" if approve else "rejected",
                "approval_granted" if approve else "approval_rejected",
            )
            return self.status(run_id)

    def _mark_interrupted(self, run_id: str, step_id: str) -> None:
        self.store.transition(
            run_id, step_id, "running", "interrupted", "attempt_interrupted",
            reason="attempt_outcome_unknown",
        )

    def recover(self, run_id: str, step_id: str, *, retry: bool) -> JsonObject:
        with self.store.run_lock(run_id):
            report = self.store.status(run_id)
            if report["status"] not in {"running", "interrupted"} or report["current_step"] != step_id:
                raise WorkflowError(f"Run {run_id} has no interrupted attempt for {step_id}")
            if report["status"] == "running":
                self._mark_interrupted(run_id, step_id)
            if retry:
                workflow = parse_workflow(self.store.run(run_id)["definition"], self.adapters)
                step = next(step for step in workflow.steps if step.id == step_id)
                assert isinstance(step, Action)
                saved = next(item for item in report["steps"] if item["step_id"] == step_id)
                if saved["attempts"] >= step.max_attempts:
                    raise WorkflowError("No attempts remain. Resolve this interruption with fail.")
            self.store.transition(
                run_id, step_id, "interrupted", "pending" if retry else "failed",
                "interruption_resolved", reason=None if retry else "operator_stopped",
                details={"decision": "retry" if retry else "fail"},
            )
            return self.status(run_id)
