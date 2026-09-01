"""Core schemas and helpers for the decomposed granularity-3 experiment.

The original block/state experiment asks one model response to contain both a
control-flow trace and run-indexed variable changes.  This module implements
the decomposed protocol described in ``DECOMPOSED_EXECUTION.md``:

* one control-flow task per concrete program input;
* one Oracle-control-flow state task per tracked variable;
* optional predicted-control-flow state tasks for end-to-end propagation.

The local runtime Oracle remains shared.  Only the model-facing tasks and the
evaluation projections are separated.
"""

from __future__ import annotations

import json
from pathlib import Path

from granularity3_local.block_state_local import (
    COMPACT_UNDEFINED,
    UNDEFINED,
    compact_value,
    flat_run_traces_equal,
)


SCHEMA_VERSION = "g3-decomposed-v2"
CONTROL_FLOW_KIND = "control_flow"
ORACLE_STATE_KIND = "oracle_state"
PREDICTED_STATE_KIND = "predicted_state"
STATE_KINDS = {ORACLE_STATE_KIND, PREDICTED_STATE_KIND}


CONTROL_FLOW_SYSTEM_PROMPT = """You predict only the concrete control flow of one Python function call.

Input fields:
- fn: target function signature.
- args: positional arguments for this call.
- blocks: target-function blocks. Each row is [block_id, source, outgoing_edges].
- outgoing_edges: [edge_type, target_block] rows; null target means termination.
- ctx: optional helper functions, imports, or constants. Treat helper calls atomically.

Return exactly one JSON object with one field named trace.
trace is a flat list of [path, repeat_count] rows:
- path contains one or more provided block IDs joined by >.
- repeat_count is a positive integer.
- Preserve concrete execution order and all loop-test occurrences.
- One loop iteration starts at its loop header and ends immediately before the next execution of that header.
- Merge only adjacent loop iterations whose complete paths are identical.
- Keep the final loop test that takes the loop-exit edge as a separate [header,1] row.

Example:
{"trace":[["B001",1],["B002>B003",3],["B002",1],["B004",1]]}

Return JSON only. Do not return variable states, changes, explanations, Markdown, or any additional field."""


STATE_SYSTEM_PROMPT = """You track the state of exactly one target variable during one concrete Python function call.

Input fields:
- fn: target function signature.
- args: positional arguments for this call.
- blocks: target-function blocks. Each row is [block_id, source, outgoing_edges].
- execution_trace: the concrete block path, encoded as [path, repeat_count] rows.
- target_variable: the only variable whose state you must return.
- ctx: optional helper functions, imports, or constants. Treat helper calls atomically.

Treat execution_trace as the fixed path for this question. Follow it exactly;
do not infer, repair, or replace it with another path.

Return exactly one JSON object with one field named states.
states must contain:
1. the target variable's value at function entry, or {"$u":1} if it is not yet defined;
2. the new value after every actual change to that variable, in execution order.

Do not repeat a value when a block only reads the variable or assigns an equal value.
Do not include block IDs, step indexes, variable names, before/after pairs, or other variables.

Value encoding:
- undefined local: {"$u":1}
- Python list: JSON array
- tuple: {"$t":[...]}
- dict: {"$d":[[key,value],...]}
- set: {"$s":[...]}
- special float: {"$f":"..."}

Example:
{"states":[{"$u":1},1,2,3]}

Return JSON only. Do not return explanations, Markdown, or any additional field."""


class ResponseValidationError(ValueError):
    """Raised when a model response does not follow the decomposed protocol."""


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def read_jsonl(path):
    path = Path(path)
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def compact_json(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def task_sort_key(task_id):
    try:
        return (0, int(str(task_id).rsplit("_", 1)[1]))
    except (IndexError, ValueError):
        return (1, str(task_id))


def input_sort_key(input_id):
    try:
        return (0, int(str(input_id).rsplit("_", 1)[1]))
    except (IndexError, ValueError):
        return (1, str(input_id))


def prompt_for_kind(kind):
    if kind == CONTROL_FLOW_KIND:
        return CONTROL_FLOW_SYSTEM_PROMPT
    if kind in STATE_KINDS:
        return STATE_SYSTEM_PROMPT
    raise ValueError(f"unknown decomposed task kind: {kind}")


def build_messages(request_record):
    kind = request_record["kind"]
    return [
        {"role": "system", "content": prompt_for_kind(kind)},
        {
            "role": "user",
            "content": compact_json(request_record["request"]),
        },
    ]


def state_sequences_from_events(events, include_unchanged=False):
    """Project runtime events to one independent state sequence per variable.

    Each sequence contains the function-entry value followed by every value in
    the variable's dynamic deltas.  Block positions are intentionally omitted:
    the state task is conditioned on a supplied execution trace and measures
    value maintenance, not trace prediction or output alignment.
    """
    if not events:
        return {}

    initial = events[0].get("state_before") or {}
    changed_variables = {
        variable
        for event in events
        for variable in (event.get("state_delta") or {})
    }
    variables = set(changed_variables)
    if include_unchanged:
        for event in events:
            variables.update((event.get("state_before") or {}).keys())
            variables.update((event.get("state_after") or {}).keys())

    result = {}
    for variable in sorted(variables):
        states = [compact_value(initial.get(variable, UNDEFINED))]
        for event in events:
            delta = (event.get("state_delta") or {}).get(variable)
            if delta is None:
                continue
            new_value = compact_value(delta["after"])
            if new_value != states[-1]:
                states.append(new_value)
        if include_unchanged or variable in changed_variables:
            result[variable] = states
    return result


def parse_response_payload(value):
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        raise ResponseValidationError("response payload must be a JSON object or string")
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as error:
        raise ResponseValidationError(
            f"response is not valid JSON: line {error.lineno} column {error.colno}"
        ) from error
    if not isinstance(payload, dict):
        raise ResponseValidationError("response JSON root must be an object")
    return payload


def response_payload_from_record(record):
    if "response" in record:
        return parse_response_payload(record["response"])
    if "raw_response" in record:
        return parse_response_payload(record["raw_response"])
    raise ResponseValidationError("response record needs response or raw_response")


def known_block_ids(request_record):
    return {
        row[0]
        for row in request_record["request"].get("blocks", [])
        if isinstance(row, list) and row
    }


def validate_trace(trace, request_record):
    if not isinstance(trace, list) or not trace:
        raise ResponseValidationError("trace must be a non-empty list")
    allowed = known_block_ids(request_record)
    normalized = []
    for index, row in enumerate(trace):
        if not isinstance(row, list) or len(row) != 2:
            raise ResponseValidationError(
                f"trace[{index}] must be [path, repeat_count]"
            )
        path, repeat_count = row
        if not isinstance(path, str) or not path:
            raise ResponseValidationError(f"trace[{index}] path must be non-empty")
        if (
            isinstance(repeat_count, bool)
            or not isinstance(repeat_count, int)
            or repeat_count < 1
        ):
            raise ResponseValidationError(
                f"trace[{index}] repeat_count must be a positive integer"
            )
        blocks = path.split(">")
        if any(not block for block in blocks):
            raise ResponseValidationError(f"trace[{index}] contains an empty block id")
        unknown = [block for block in blocks if block not in allowed]
        if unknown:
            raise ResponseValidationError(
                f"trace[{index}] contains unknown block ids: {unknown}"
            )
        normalized.append([path, repeat_count])
    return normalized


def trace_is_canonical(trace):
    """Return false for the simplest non-canonical adjacent run split."""
    return all(
        trace[index - 1][0] != trace[index][0]
        for index in range(1, len(trace))
    )


def validate_response(request_record, value):
    payload = parse_response_payload(value)
    kind = request_record["kind"]
    if kind == CONTROL_FLOW_KIND:
        if set(payload) != {"trace"}:
            raise ResponseValidationError(
                "control-flow response must contain only trace"
            )
        return {
            "trace": validate_trace(payload["trace"], request_record),
        }
    if kind in STATE_KINDS:
        if set(payload) != {"states"}:
            raise ResponseValidationError("state response must contain only states")
        states = payload["states"]
        if not isinstance(states, list) or not states:
            raise ResponseValidationError("states must be a non-empty list")
        return {"states": states}
    raise ResponseValidationError(f"unknown request kind: {kind}")


def score_control_flow(predicted, oracle):
    predicted_trace = predicted["trace"]
    oracle_trace = oracle["trace"]
    return {
        "canonical_format_valid": trace_is_canonical(predicted_trace),
        "canonical_trace_exact": predicted_trace == oracle_trace,
        "expanded_trace_exact": flat_run_traces_equal(
            predicted_trace,
            oracle_trace,
        ),
        "predicted_run_count": len(predicted_trace),
        "oracle_run_count": len(oracle_trace),
        "first_run_difference": first_difference(predicted_trace, oracle_trace),
    }


def score_state(predicted, oracle):
    predicted_states = predicted["states"]
    oracle_states = oracle["states"]
    width = max(len(predicted_states), len(oracle_states))
    matching_positions = sum(
        left == right
        for left, right in zip(predicted_states, oracle_states)
    )
    prefix_length = first_difference(predicted_states, oracle_states)
    if prefix_length is None:
        prefix_length = len(oracle_states)
    return {
        "state_exact": predicted_states == oracle_states,
        "state_position_accuracy": matching_positions / width if width else 1.0,
        "matching_state_positions": matching_positions,
        "predicted_state_count": len(predicted_states),
        "oracle_state_count": len(oracle_states),
        "correct_prefix_length": prefix_length,
        "first_state_difference": first_difference(
            predicted_states,
            oracle_states,
        ),
    }


def first_difference(left, right):
    for index, (left_item, right_item) in enumerate(zip(left, right)):
        if left_item != right_item:
            return index
    if len(left) != len(right):
        return min(len(left), len(right))
    return None


def make_oracle_response(oracle_record):
    """Create a response record for deterministic local evaluator self-checks."""
    return {
        "request_id": oracle_record["request_id"],
        "response": oracle_record["answer"],
        "source": "local_oracle_self_check",
    }


def undefined_value():
    return dict(COMPACT_UNDEFINED)
