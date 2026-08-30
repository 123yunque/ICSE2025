"""Prepare the decomposed control-flow and per-variable state datasets."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
from collections import defaultdict
from pathlib import Path

from granularity3_local.block_state_batch import extract_static_context
from granularity3_local.block_state_local import build_flat_run_answer
from granularity3_local.decomposed_core import (
    CONTROL_FLOW_KIND,
    CONTROL_FLOW_SYSTEM_PROMPT,
    ORACLE_STATE_KIND,
    PREDICTED_STATE_KIND,
    SCHEMA_VERSION,
    STATE_SYSTEM_PROMPT,
    input_sort_key,
    make_oracle_response,
    read_json,
    read_jsonl,
    response_payload_from_record,
    state_sequences_from_events,
    task_sort_key,
    validate_response,
)
from granularity3_local.decomposed_statement import execute_statement_state_trace
from granularity3_local.oracle import write_json, write_jsonl


PREPARE_SCHEMA_VERSION = "g3-decomposed-prepare-v1"
PREDICTED_PREPARE_SCHEMA_VERSION = "g3-decomposed-predicted-state-v1"


def latest_case_records(records):
    latest = {}
    for row in records:
        latest[row["case_key"]] = row
    return list(latest.values())


def _select_task_ids(grouped, requested, task_limit):
    available = sorted(grouped, key=task_sort_key)
    if requested:
        ordered = list(dict.fromkeys(requested))
        missing = [task_id for task_id in ordered if task_id not in grouped]
        if missing:
            raise ValueError(f"requested tasks have no successful local cases: {missing}")
    else:
        ordered = available
    if task_limit is not None:
        if task_limit < 1:
            raise ValueError("task_limit must be positive")
        ordered = ordered[:task_limit]
    return ordered


def _case_context(case_dir, function_name):
    source = (case_dir / "code.py").read_text(encoding="utf-8")
    return extract_static_context(source, function_name)


def prepare_decomposed_dataset(
    local_output_root,
    output_dir,
    task_ids=None,
    task_limit=None,
    inputs_per_task=None,
    max_events=500,
    max_statement_events=2000,
    max_state_items=500,
    include_unchanged=False,
    require_state_change=False,
):
    local_output_root = Path(local_output_root)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    records = latest_case_records(
        read_jsonl(local_output_root / "case_records.jsonl")
    )
    grouped = defaultdict(list)
    excluded = []
    for row in records:
        case_key = row.get("case_key")
        if row.get("status") not in {"success", "cached"}:
            excluded.append({
                "case_key": case_key,
                "scope": "case",
                "reason": row.get("error_type") or row.get("status"),
            })
            continue
        if max_events is not None and row.get("event_count", 0) > max_events:
            excluded.append({
                "case_key": case_key,
                "scope": "case",
                "reason": "event_limit",
                "value": row.get("event_count"),
                "limit": max_events,
            })
            continue
        if require_state_change and row.get("change_count", 0) < 1:
            excluded.append({
                "case_key": case_key,
                "scope": "case",
                "reason": "no_state_change",
            })
            continue
        grouped[row["task_id"]].append(row)

    selected_task_ids = _select_task_ids(grouped, task_ids, task_limit)
    preselection_excluded_count = len(excluded)
    selected_task_set = set(selected_task_ids)
    excluded = [
        row
        for row in excluded
        if (
            isinstance(row.get("case_key"), str)
            and row["case_key"].split("/", 1)[0] in selected_task_set
        )
    ]
    control_requests = []
    control_oracles = []
    state_requests = []
    state_oracles = []
    case_manifests = []

    for task_id in selected_task_ids:
        task_rows = sorted(
            grouped[task_id],
            key=lambda row: input_sort_key(row["input_id"]),
        )
        if inputs_per_task is not None:
            if inputs_per_task < 1:
                raise ValueError("inputs_per_task must be positive")
            task_rows = task_rows[:inputs_per_task]

        context = None
        for row in task_rows:
            input_id = row["input_id"]
            case_key = f"{task_id}/{input_id}"
            case_dir = local_output_root / "cases" / task_id / input_id
            model_input = read_json(case_dir / "model_input.json")
            local_answer = read_json(case_dir / "local_answer.json")
            events = read_jsonl(case_dir / "oracle" / "events.jsonl")
            case_manifest = read_json(case_dir / "manifest.json")
            runtime_case = read_json(case_dir / "oracle" / "case.json")
            source = (case_dir / "code.py").read_text(encoding="utf-8")
            if context is None:
                context = _case_context(case_dir, model_input["fn"].split("(", 1)[0])

            control_trace = build_flat_run_answer(
                local_answer,
                model_input["blocks"],
            )["block_trace"]
            common_request = {
                "fn": model_input["fn"],
                "args": model_input["args"],
                "blocks": model_input["blocks"],
            }
            if context:
                common_request["ctx"] = context

            control_record = {
                "schema_version": SCHEMA_VERSION,
                "request_id": case_key,
                "case_key": case_key,
                "task_id": task_id,
                "input_id": input_id,
                "kind": CONTROL_FLOW_KIND,
                "request": common_request,
            }
            control_oracle = {
                "schema_version": SCHEMA_VERSION,
                "request_id": case_key,
                "case_key": case_key,
                "task_id": task_id,
                "input_id": input_id,
                "kind": CONTROL_FLOW_KIND,
                "answer": {"trace": control_trace},
            }
            control_requests.append(control_record)
            control_oracles.append(control_oracle)

            state_status = "success"
            statement_event_count = 0
            try:
                statement_trace = execute_statement_state_trace(
                    source=source,
                    function_name=case_manifest["function"],
                    input_value=ast.literal_eval(case_manifest["normalized_call_args"]),
                    filename=str(case_dir / "code.py"),
                    max_events=max_statement_events,
                )
                statement_event_count = len(statement_trace["events"])
                if statement_trace["result"] != runtime_case["result"]:
                    raise AssertionError(
                        f"statement instrumentation changed result: "
                        f"{statement_trace['result']!r} != {runtime_case['result']!r}"
                    )
                sequences = state_sequences_from_events(
                    statement_trace["events"],
                    include_unchanged=include_unchanged,
                )
            except Exception as error:
                state_status = "excluded"
                sequences = {}
                excluded.append({
                    "case_key": case_key,
                    "scope": "state_case",
                    "reason": type(error).__name__,
                    "detail": str(error),
                })
            kept_variables = []
            for variable_index, variable in enumerate(sorted(sequences), start=1):
                states = sequences[variable]
                if max_state_items is not None and len(states) > max_state_items:
                    excluded.append({
                        "case_key": case_key,
                        "scope": "variable",
                        "target_variable": variable,
                        "reason": "state_item_limit",
                        "value": len(states),
                        "limit": max_state_items,
                    })
                    continue
                request_id = f"{case_key}/state/{variable_index:03d}"
                state_request = {
                    **common_request,
                    "execution_trace": control_trace,
                    "trace_source": "oracle",
                    "target_variable": variable,
                }
                state_requests.append({
                    "schema_version": SCHEMA_VERSION,
                    "request_id": request_id,
                    "case_key": case_key,
                    "task_id": task_id,
                    "input_id": input_id,
                    "kind": ORACLE_STATE_KIND,
                    "target_variable": variable,
                    "control_request_id": case_key,
                    "request": state_request,
                })
                state_oracles.append({
                    "schema_version": SCHEMA_VERSION,
                    "request_id": request_id,
                    "case_key": case_key,
                    "task_id": task_id,
                    "input_id": input_id,
                    "kind": ORACLE_STATE_KIND,
                    "target_variable": variable,
                    "control_request_id": case_key,
                    "answer": {"states": states},
                })
                kept_variables.append(variable)

            case_manifests.append({
                "case_key": case_key,
                "task_id": task_id,
                "input_id": input_id,
                "event_count": len(events),
                "statement_event_count": statement_event_count,
                "state_status": state_status,
                "control_run_count": len(control_trace),
                "tracked_variable_count": len(kept_variables),
                "tracked_variables": kept_variables,
            })

    control_dir = output_dir / CONTROL_FLOW_KIND
    state_dir = output_dir / ORACLE_STATE_KIND
    control_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    write_jsonl(control_dir / "requests.jsonl", control_requests)
    write_jsonl(control_dir / "oracles.jsonl", control_oracles)
    write_jsonl(
        control_dir / "oracle_responses.jsonl",
        [make_oracle_response(row) for row in control_oracles],
    )
    write_jsonl(state_dir / "requests.jsonl", state_requests)
    write_jsonl(state_dir / "oracles.jsonl", state_oracles)
    write_jsonl(
        state_dir / "oracle_responses.jsonl",
        [make_oracle_response(row) for row in state_oracles],
    )
    write_jsonl(output_dir / "case_manifest.jsonl", case_manifests)
    write_jsonl(output_dir / "excluded.jsonl", excluded)
    (control_dir / "system_prompt.txt").write_text(
        CONTROL_FLOW_SYSTEM_PROMPT + "\n",
        encoding="utf-8",
    )
    (state_dir / "system_prompt.txt").write_text(
        STATE_SYSTEM_PROMPT + "\n",
        encoding="utf-8",
    )

    summary = {
        "schema_version": PREPARE_SCHEMA_VERSION,
        "protocol_schema_version": SCHEMA_VERSION,
        "local_output_root": str(local_output_root.resolve()),
        "output_dir": str(output_dir.resolve()),
        "selected_tasks": selected_task_ids,
        "task_count": len(selected_task_ids),
        "case_count": len(control_requests),
        "control_flow_request_count": len(control_requests),
        "oracle_state_request_count": len(state_requests),
        "tracked_variable_count": len(state_requests),
        "excluded_count": len(excluded),
        "preselection_excluded_count": preselection_excluded_count,
        "include_unchanged": include_unchanged,
        "require_state_change": require_state_change,
        "max_events": max_events,
        "max_statement_events": max_statement_events,
        "max_state_items": max_state_items,
        "inputs_per_task": inputs_per_task,
        "control_prompt_sha256": hashlib.sha256(
            CONTROL_FLOW_SYSTEM_PROMPT.encode("utf-8")
        ).hexdigest(),
        "state_prompt_sha256": hashlib.sha256(
            STATE_SYSTEM_PROMPT.encode("utf-8")
        ).hexdigest(),
    }
    write_json(output_dir / "summary.json", summary)
    return {
        "summary": summary,
        "control_requests": control_requests,
        "control_oracles": control_oracles,
        "state_requests": state_requests,
        "state_oracles": state_oracles,
        "excluded": excluded,
    }


def prepare_predicted_state_dataset(
    control_requests,
    control_responses,
    state_requests,
    state_oracles,
    output_dir,
):
    """Replace Oracle traces with valid model-predicted traces for E2E state tasks."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    control_by_id = {row["request_id"]: row for row in control_requests}
    response_by_id = {row["request_id"]: row for row in control_responses}
    oracle_by_id = {row["request_id"]: row for row in state_oracles}

    predicted_trace_by_case = {}
    control_errors = {}
    for request_id, request_record in control_by_id.items():
        response_record = response_by_id.get(request_id)
        if response_record is None:
            control_errors[request_id] = "missing_control_response"
            continue
        try:
            payload = response_payload_from_record(response_record)
            predicted = validate_response(request_record, payload)
        except Exception as error:
            control_errors[request_id] = f"invalid_control_response: {error}"
            continue
        predicted_trace_by_case[request_record["case_key"]] = predicted["trace"]

    predicted_requests = []
    predicted_oracles = []
    excluded = []
    for state_record in state_requests:
        case_key = state_record["case_key"]
        parent_request_id = state_record["request_id"]
        trace = predicted_trace_by_case.get(case_key)
        if trace is None:
            excluded.append({
                "request_id": parent_request_id,
                "case_key": case_key,
                "target_variable": state_record["target_variable"],
                "reason": control_errors.get(case_key, "missing_control_response"),
            })
            continue
        request_id = parent_request_id.replace("/state/", "/predicted_state/", 1)
        request = dict(state_record["request"])
        request["execution_trace"] = trace
        request["trace_source"] = "predicted"
        predicted_requests.append({
            **{key: value for key, value in state_record.items() if key != "request"},
            "request_id": request_id,
            "kind": PREDICTED_STATE_KIND,
            "parent_state_request_id": parent_request_id,
            "request": request,
        })
        oracle = oracle_by_id[parent_request_id]
        predicted_oracles.append({
            **oracle,
            "request_id": request_id,
            "kind": PREDICTED_STATE_KIND,
            "parent_state_request_id": parent_request_id,
        })

    write_jsonl(output_dir / "requests.jsonl", predicted_requests)
    write_jsonl(output_dir / "oracles.jsonl", predicted_oracles)
    write_jsonl(
        output_dir / "oracle_responses.jsonl",
        [make_oracle_response(row) for row in predicted_oracles],
    )
    write_jsonl(output_dir / "excluded.jsonl", excluded)
    (output_dir / "system_prompt.txt").write_text(
        STATE_SYSTEM_PROMPT + "\n",
        encoding="utf-8",
    )
    summary = {
        "schema_version": PREDICTED_PREPARE_SCHEMA_VERSION,
        "control_request_count": len(control_requests),
        "valid_control_trace_count": len(predicted_trace_by_case),
        "source_state_request_count": len(state_requests),
        "predicted_state_request_count": len(predicted_requests),
        "excluded_state_request_count": len(excluded),
    }
    write_json(output_dir / "summary.json", summary)
    return {
        "summary": summary,
        "requests": predicted_requests,
        "oracles": predicted_oracles,
        "excluded": excluded,
    }


def _parse_task_ids(value):
    if not value:
        return None
    return [item.strip() for item in value.split(",") if item.strip()]


def main():
    parser = argparse.ArgumentParser(
        description="Prepare decomposed control-flow and variable-state tasks."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare", help="Build Oracle-CF decomposed tasks.")
    prepare.add_argument("--local-output-root", required=True)
    prepare.add_argument("--output-dir", required=True)
    prepare.add_argument("--tasks", help="Comma-separated task ids.")
    prepare.add_argument("--task-limit", type=int)
    prepare.add_argument("--inputs-per-task", type=int)
    prepare.add_argument("--max-events", type=int, default=500)
    prepare.add_argument("--max-statement-events", type=int, default=2000)
    prepare.add_argument("--max-state-items", type=int, default=500)
    prepare.add_argument("--include-unchanged", action="store_true")
    prepare.add_argument(
        "--require-state-change",
        action="store_true",
        help="Pilot-only filter: select cases whose local Oracle has at least one change.",
    )

    predicted = subparsers.add_parser(
        "predicted-state",
        help="Build state tasks conditioned on model-predicted control flow.",
    )
    predicted.add_argument("--control-requests", required=True)
    predicted.add_argument("--control-responses", required=True)
    predicted.add_argument("--state-requests", required=True)
    predicted.add_argument("--state-oracles", required=True)
    predicted.add_argument("--output-dir", required=True)

    args = parser.parse_args()
    if args.command == "prepare":
        result = prepare_decomposed_dataset(
            local_output_root=args.local_output_root,
            output_dir=args.output_dir,
            task_ids=_parse_task_ids(args.tasks),
            task_limit=args.task_limit,
            inputs_per_task=args.inputs_per_task,
            max_events=args.max_events,
            max_statement_events=args.max_statement_events,
            max_state_items=args.max_state_items,
            include_unchanged=args.include_unchanged,
            require_state_change=args.require_state_change,
        )
    else:
        result = prepare_predicted_state_dataset(
            control_requests=read_jsonl(args.control_requests),
            control_responses=read_jsonl(args.control_responses),
            state_requests=read_jsonl(args.state_requests),
            state_oracles=read_jsonl(args.state_oracles),
            output_dir=args.output_dir,
        )
    print(json.dumps(result["summary"], ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
