"""Workflow definitions and deterministic output checks."""

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

JsonObject = dict[str, Any]
IDENTIFIER = re.compile(r"[a-z][a-z0-9_-]{0,63}")
MAX_OUTPUT_BYTES = 256 * 1024
TYPES = {
    "array": (list,),
    "boolean": (bool,),
    "integer": (int,),
    "null": (type(None),),
    "number": (int, float),
    "object": (dict,),
    "string": (str,),
}


class WorkflowError(ValueError):
    """A definition or operator input is invalid."""


class ActionError(Exception):
    """An attempt failed with a log-safe reason."""

    def __init__(self, reason: str, *, retryable: bool = True, exit_code: int | None = None):
        super().__init__(reason)
        self.reason = reason
        self.retryable = retryable
        self.exit_code = exit_code


@dataclass(frozen=True)
class Context:
    run_id: str
    step_id: str
    workspace: Path
    attempt: int
    timeout_seconds: float
    inputs: dict[str, JsonObject]


class Adapter(Protocol):
    """Validate config and run one bounded attempt."""

    def check(self, config: JsonObject, location: str) -> None: ...

    def execute(self, config: JsonObject, context: Context) -> JsonObject: ...


@dataclass(frozen=True)
class Action:
    id: str
    adapter: str
    config: JsonObject
    timeout_seconds: float
    max_attempts: int
    from_steps: tuple[str, ...]
    checks: JsonObject


@dataclass(frozen=True)
class Approval:
    id: str
    message: str


@dataclass(frozen=True)
class Workflow:
    name: str
    steps: tuple[Action | Approval, ...]
    snapshot: str = field(repr=False)


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True)


def load_json(text: str) -> Any:
    def unique_keys(items: list[tuple[str, Any]]) -> JsonObject:
        result: JsonObject = {}
        for key, value in items:
            if key in result:
                raise WorkflowError("JSON object contains a duplicate key")
            result[key] = value
        return result

    def parse_number(token: str) -> float:
        value = float(token)
        if not math.isfinite(value):
            raise WorkflowError("JSON numbers must be finite")
        return value

    try:
        return json.loads(
            text, object_pairs_hook=unique_keys, parse_float=parse_number,
            parse_constant=parse_number,
        )
    except (ValueError, RecursionError) as error:
        if isinstance(error, WorkflowError):
            raise
        raise WorkflowError("Invalid JSON or unsupported nesting") from error


def object_with(
    value: Any, location: str, required: set[str], optional: set[str] | None = None,
) -> JsonObject:
    if type(value) is not dict:
        raise WorkflowError(f"{location} must be an object")
    missing = required - value.keys()
    unknown = value.keys() - required - (optional or set())
    if missing:
        raise WorkflowError(f"{location} is missing fields: {', '.join(sorted(missing))}")
    if unknown:
        raise WorkflowError(f"{location} has unknown fields: {', '.join(sorted(unknown))}")
    return value


def nonempty_text(value: Any, location: str) -> str:
    if type(value) is not str or not value.strip() or "\0" in value:
        raise WorkflowError(f"{location} must be a nonempty string without NUL")
    return value


def _checks(value: Any, location: str) -> JsonObject:
    checks = object_with(value, location, set(), {"types", "equals", "nonempty"})
    types = checks.get("types", {})
    equals = checks.get("equals", {})
    nonempty = checks.get("nonempty", [])
    if type(types) is not dict or type(equals) is not dict:
        raise WorkflowError(f"{location}.types and .equals must be objects")
    for kind in types.values():
        if type(kind) is not str or kind not in TYPES:
            raise WorkflowError(f"{location}.types contains an unsupported JSON type")
    if type(nonempty) is not list or any(type(key) is not str for key in nonempty):
        raise WorkflowError(f"{location}.nonempty must be a list of field names")
    for key, expected in equals.items():
        if key in types and type(expected) not in TYPES[types[key]]:
            raise WorkflowError(f"{location} has incompatible types and equals for {key!r}")
    for key in nonempty:
        if key in types and types[key] not in {"string", "array", "object"}:
            raise WorkflowError(f"{location} has an incompatible nonempty check for {key!r}")
        if key in equals and not _nonempty(equals[key]):
            raise WorkflowError(f"{location} has incompatible nonempty and equals for {key!r}")
    return checks


def _nonempty(value: Any) -> bool:
    if type(value) is str:
        return bool(value.strip())
    return type(value) in (list, dict) and bool(value)


def validate_output(output: Any, checks: JsonObject) -> str:
    if type(output) is not dict:
        raise ActionError("output_not_object")
    try:
        serialized = json_text(output)
    except (TypeError, ValueError, RecursionError) as error:
        raise ActionError("output_not_json") from error
    if len(serialized) > MAX_OUTPUT_BYTES:
        raise ActionError("output_too_large")
    for key, kind in checks.get("types", {}).items():
        if key not in output or type(output[key]) not in TYPES[kind]:
            raise ActionError("output_type_check_failed")
    for key, expected in checks.get("equals", {}).items():
        if key not in output or json_text(output[key]) != json_text(expected):
            raise ActionError("output_equality_check_failed")
    for key in checks.get("nonempty", []):
        if not _nonempty(output.get(key)):
            raise ActionError("output_nonempty_check_failed")
    return serialized


def parse_workflow(text: str, adapters: Mapping[str, Adapter]) -> Workflow:
    raw = object_with(load_json(text), "workflow", {"version", "name", "steps"})
    if type(raw["version"]) is not int or raw["version"] != 1:
        raise WorkflowError("workflow.version must be 1")
    name = nonempty_text(raw["name"], "workflow.name")
    if type(raw["steps"]) is not list or not raw["steps"]:
        raise WorkflowError("workflow.steps must be a nonempty array")

    steps: list[Action | Approval] = []
    seen: set[str] = set()
    prior_actions: set[str] = set()
    for index, value in enumerate(raw["steps"]):
        location = f"steps[{index}]"
        if type(value) is not dict:
            raise WorkflowError(f"{location} must be an object")
        kind = value.get("type")
        if kind == "approval":
            step = object_with(value, location, {"id", "type", "message"})
        elif kind == "action":
            step = object_with(
                value, location, {"id", "type", "adapter", "config"},
                {"timeout_seconds", "max_attempts", "from_steps", "validate"},
            )
        else:
            raise WorkflowError(f"{location}.type must be action or approval")

        step_id = step["id"]
        if type(step_id) is not str or not IDENTIFIER.fullmatch(step_id):
            raise WorkflowError(f"{location}.id must match {IDENTIFIER.pattern}")
        if step_id in seen:
            raise WorkflowError(f"Duplicate step id: {step_id}")
        seen.add(step_id)
        if kind == "approval":
            steps.append(Approval(step_id, nonempty_text(step["message"], f"{location}.message")))
            continue

        adapter = step["adapter"]
        if type(adapter) is not str or adapter not in adapters:
            raise WorkflowError(f"{location}.adapter is unknown")
        config = step["config"]
        if type(config) is not dict:
            raise WorkflowError(f"{location}.config must be an object")
        adapters[adapter].check(config, f"{location}.config")

        attempts = step.get("max_attempts", 1)
        if type(attempts) is not int or not 1 <= attempts <= 5:
            raise WorkflowError(f"{location}.max_attempts must be an integer from 1 to 5")
        timeout = step.get("timeout_seconds", 60)
        if type(timeout) not in (int, float) or not 0.1 <= timeout <= 1800:
            raise WorkflowError(f"{location}.timeout_seconds must be from 0.1 to 1800")
        inputs = step.get("from_steps", [])
        if type(inputs) is not list or any(
            type(key) is not str or key not in prior_actions for key in inputs
        ):
            raise WorkflowError(f"{location}.from_steps must name earlier action steps")
        if len(inputs) != len(set(inputs)):
            raise WorkflowError(f"{location}.from_steps contains duplicates")

        checks = _checks(step.get("validate", {}), f"{location}.validate")
        steps.append(Action(step_id, adapter, config, float(timeout), attempts, tuple(inputs), checks))
        prior_actions.add(step_id)
    return Workflow(name, tuple(steps), json_text(raw))
