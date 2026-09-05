import argparse
import ast
import hashlib
import json
import shutil
from collections import defaultdict
from math import gcd
from pathlib import Path

from granularity3_local.oracle import build_oracle_case, write_json
from granularity3_local.preflight import resolve_task_function
from granularity3_local.state import canonicalize


SCHEMA_VERSION = "g3-block-state-local-v1"
UNDEFINED = {"$undefined": True}
COMPACT_UNDEFINED = {"$u": 1}
EDGE_ORDER = {
    "fallthrough": 0,
    "branch_true": 1,
    "branch_false": 2,
    "loop_body": 3,
    "backedge": 4,
    "loop_exit": 5,
    "return": 6,
}


def parse_dataset_args(input_text):
    """Parse one standardized dataset row into positional arguments.

    MBPP+ stores a call's positional arguments in an outer list, while older
    generated cases may use a tuple. A scalar row is treated as one argument.
    """
    value = ast.literal_eval(input_text)
    if isinstance(value, (list, tuple)):
        return tuple(value)
    return (value,)


def compact_value(value):
    """Shorten a canonical state value without losing Python container types."""
    if isinstance(value, list):
        return [compact_value(item) for item in value]
    if not isinstance(value, dict):
        return value
    if value == UNDEFINED:
        return dict(COMPACT_UNDEFINED)
    if value.get("$type") == "list" and set(value) == {"$type", "items"}:
        return [compact_value(item) for item in value["items"]]
    if value.get("$type") == "tuple" and set(value) == {"$type", "items"}:
        return {"$t": [compact_value(item) for item in value["items"]]}
    if value.get("$type") == "dict" and set(value) == {"$type", "items"}:
        return {
            "$d": [
                [compact_value(key), compact_value(item)]
                for key, item in value["items"]
            ]
        }
    if value.get("$type") == "set" and set(value) == {"$type", "items"}:
        return {"$s": [compact_value(item) for item in value["items"]]}
    if set(value) == {"$float"}:
        return {"$f": value["$float"]}
    if value == {"$cycle": True}:
        return {"$c": 1}
    if "$type" in value and "$repr" in value and set(value) == {"$type", "$repr"}:
        return {"$o": [value["$type"], value["$repr"]]}
    return {key: compact_value(item) for key, item in value.items()}


def expand_value(value):
    """Expand a compact value back to the deterministic canonical form."""
    if isinstance(value, list):
        return {"$type": "list", "items": [expand_value(item) for item in value]}
    if not isinstance(value, dict):
        return value
    if value == COMPACT_UNDEFINED:
        return dict(UNDEFINED)
    if set(value) == {"$t"}:
        return {"$type": "tuple", "items": [expand_value(item) for item in value["$t"]]}
    if set(value) == {"$d"}:
        return {
            "$type": "dict",
            "items": [
                [expand_value(key), expand_value(item)]
                for key, item in value["$d"]
            ],
        }
    if set(value) == {"$s"}:
        return {"$type": "set", "items": [expand_value(item) for item in value["$s"]]}
    if set(value) == {"$f"}:
        return {"$float": value["$f"]}
    if value == {"$c": 1}:
        return {"$cycle": True}
    if set(value) == {"$o"} and isinstance(value["$o"], list) and len(value["$o"]) == 2:
        return {"$type": value["$o"][0], "$repr": value["$o"][1]}
    return {key: expand_value(item) for key, item in value.items()}


def function_signature(source, function_name):
    tree = ast.parse(source)
    target = next(
        (
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name
        ),
        None,
    )
    if target is None:
        raise ValueError(f"function not found: {function_name}")
    return f"{function_name}({ast.unparse(target.args)})"


def build_model_input(source, function_name, args, cfg):
    """Build the static-only, token-efficient input for the later LLM stage."""
    outgoing = defaultdict(list)
    for edge in cfg["edges"]:
        outgoing[edge["from"]].append([edge["edge_type"], edge["to"]])
    for rows in outgoing.values():
        rows.sort(key=lambda row: (EDGE_ORDER.get(row[0], 99), "" if row[1] is None else row[1]))

    blocks = []
    for block_id, block in sorted(cfg["blocks"].items()):
        blocks.append([block_id, block["source"], outgoing.get(block_id, [])])
    return {
        "fn": function_signature(source, function_name),
        "args": [compact_value(canonicalize(value)) for value in args],
        "blocks": blocks,
    }


def build_local_answer(events):
    """Project the full runtime oracle onto block order and sparse state changes."""
    block_trace = []
    changes = []
    for step, event in enumerate(events):
        block_trace.append(event["block_id"])
        for variable, delta in sorted(event["state_delta"].items()):
            changes.append([
                step,
                variable,
                compact_value(delta["before"]),
                compact_value(delta["after"]),
            ])
    return {"block_trace": block_trace, "changes": changes}


def _parse_flat_run_trace(block_trace):
    parsed = []
    for run_index, row in enumerate(block_trace):
        if not isinstance(row, list) or len(row) != 2:
            raise ValueError(f"block_trace[{run_index}] must be [path, repeat_count]")
        path, repeat_count = row
        if not isinstance(path, str) or not path:
            raise ValueError(f"block_trace[{run_index}] path must be a non-empty string")
        if isinstance(repeat_count, bool) or not isinstance(repeat_count, int) or repeat_count < 1:
            raise ValueError(f"block_trace[{run_index}] repeat_count must be a positive integer")
        pattern = tuple(path.split(">"))
        if any(not block_id for block_id in pattern):
            raise ValueError(f"block_trace[{run_index}] path contains an empty block id")
        parsed.append((pattern, repeat_count))
    return parsed


def flat_run_trace_length(block_trace):
    """Return expanded Block count without materializing repeated loop iterations."""
    return sum(len(pattern) * repeat_count for pattern, repeat_count in _parse_flat_run_trace(block_trace))


def _periodic_prefix_matches(
    left_pattern,
    left_offset,
    right_pattern,
    right_offset,
    length,
):
    """Compare periodic prefixes exactly, independent of their repeat counts.

    Fine-Wilf's periodicity bound means two periodic sequences that agree for
    p + q - gcd(p, q) symbols agree forever. Path lengths, rather than loop
    repeat counts, therefore bound the work performed here.
    """
    if length < 1:
        return True
    left_period = len(left_pattern)
    right_period = len(right_pattern)
    if left_pattern == right_pattern and left_offset % left_period == right_offset % right_period:
        return True
    comparison_length = min(
        length,
        left_period + right_period - gcd(left_period, right_period),
    )
    return all(
        left_pattern[(left_offset + index) % left_period]
        == right_pattern[(right_offset + index) % right_period]
        for index in range(comparison_length)
    )


def flat_run_traces_equal(left_trace, right_trace):
    """Compare expanded Block sequences exactly without expanding long loops."""
    left_rows = _parse_flat_run_trace(left_trace)
    right_rows = _parse_flat_run_trace(right_trace)
    left_length = sum(len(pattern) * count for pattern, count in left_rows)
    right_length = sum(len(pattern) * count for pattern, count in right_rows)
    if left_length != right_length:
        return False

    left_index = right_index = 0
    left_consumed = right_consumed = 0
    while left_index < len(left_rows) and right_index < len(right_rows):
        left_pattern, left_count = left_rows[left_index]
        right_pattern, right_count = right_rows[right_index]
        left_total = len(left_pattern) * left_count
        right_total = len(right_pattern) * right_count
        common_length = min(
            left_total - left_consumed,
            right_total - right_consumed,
        )
        if not _periodic_prefix_matches(
            left_pattern,
            left_consumed % len(left_pattern),
            right_pattern,
            right_consumed % len(right_pattern),
            common_length,
        ):
            return False

        left_consumed += common_length
        right_consumed += common_length
        if left_consumed == left_total:
            left_index += 1
            left_consumed = 0
        if right_consumed == right_total:
            right_index += 1
            right_consumed = 0

    return left_index == len(left_rows) and right_index == len(right_rows)


def expand_flat_run_trace(block_trace):
    """Expand small canonical traces for debugging; scoring must use compressed helpers."""
    expanded = []
    for blocks, repeat_count in _parse_flat_run_trace(block_trace):
        for _ in range(repeat_count):
            expanded.extend(blocks)
    return expanded


def build_flat_run_answer(local_answer, blocks):
    """Compress loop iterations into flat path runs and aggregate state at run boundaries.

    Non-loop blocks remain one row each. A loop iteration starts at a block with
    a loop_body edge and ends immediately before the next execution of that
    header. Only adjacent identical iteration paths are merged.
    """
    trace = list(local_answer["block_trace"])
    loop_body_targets = {}
    for block_id, _source, outgoing_edges in blocks:
        targets = {
            target
            for edge_type, target in outgoing_edges
            if edge_type == "loop_body" and target is not None
        }
        if targets:
            loop_body_targets[block_id] = targets

    rows = []
    spans = []
    index = 0
    while index < len(trace):
        header = trace[index]
        body_targets = loop_body_targets.get(header, set())
        enters_body = index + 1 < len(trace) and trace[index + 1] in body_targets
        next_header = None
        if enters_body:
            try:
                next_header = trace.index(header, index + 1)
            except ValueError:
                next_header = None

        if next_header is not None:
            path = trace[index:next_header]
            repeat_count = 1
            end = next_header
            while end + 1 < len(trace) and trace[end + 1] in body_targets:
                try:
                    candidate_end = trace.index(header, end + 1)
                except ValueError:
                    break
                if trace[end:candidate_end] != path:
                    break
                repeat_count += 1
                end = candidate_end
            rows.append([">".join(path), repeat_count])
            spans.append((index, end))
            index = end
            continue

        rows.append([header, 1])
        spans.append((index, index + 1))
        index += 1

    if expand_flat_run_trace(rows) != trace:
        raise AssertionError("flat path runs do not reconstruct the original block trace")

    step_to_run = [None] * len(trace)
    for run_index, (start, end) in enumerate(spans):
        for step in range(start, end):
            step_to_run[step] = run_index

    aggregated = defaultdict(dict)
    for step, variable, before, after in local_answer["changes"]:
        run_index = step_to_run[step]
        current = aggregated[run_index].get(variable)
        if current is None:
            aggregated[run_index][variable] = [before, after]
        else:
            current[1] = after

    changes = []
    for run_index in sorted(aggregated):
        for variable in sorted(aggregated[run_index]):
            before, after = aggregated[run_index][variable]
            changes.append([run_index, variable, before, after])
    return {"block_trace": rows, "changes": changes}


def compact_json(payload):
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def payload_hash(payload):
    return hashlib.sha256(compact_json(payload).encode("utf-8")).hexdigest()


def prepare_local_case(
    source,
    function_name,
    input_text,
    task_id,
    input_id,
    output_dir,
    source_path=None,
    variant="original",
    max_events=10000,
    max_trace_bytes=None,
):
    """Run one case locally and preserve both the raw and compact oracles."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    copied_source = output_dir / "code.py"
    if source_path is not None:
        source_path = Path(source_path)
        if source_path.resolve() != copied_source.resolve():
            shutil.copy2(source_path, copied_source)
    else:
        copied_source.write_text(source, encoding="utf-8")

    args = parse_dataset_args(input_text)
    normalized_input = repr(args)
    raw_oracle = build_oracle_case(
        source=source,
        function_name=function_name,
        input_text=normalized_input,
        task_id=task_id,
        variant=variant,
        input_id=input_id,
        output_dir=output_dir / "oracle",
        source_path=copied_source,
        max_events=max_events,
        max_trace_bytes=max_trace_bytes,
    )
    model_input = build_model_input(source, function_name, args, raw_oracle["cfg"])
    local_answer = build_local_answer(raw_oracle["events"])
    model_input_path = output_dir / "model_input.json"
    local_answer_path = output_dir / "local_answer.json"
    write_json(model_input_path, model_input)
    write_json(local_answer_path, local_answer)

    changed_steps = {row[0] for row in local_answer["changes"]}
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "case_id": raw_oracle["case"]["case_id"],
        "task_id": task_id,
        "variant": variant,
        "input_id": input_id,
        "dataset_input": input_text,
        "normalized_call_args": normalized_input,
        "function": function_name,
        "step_index_base": 0,
        "change_row_format": ["step", "variable", "before", "after"],
        "undefined_encoding": COMPACT_UNDEFINED,
        "event_count": len(local_answer["block_trace"]),
        "change_count": len(local_answer["changes"]),
        "changed_step_count": len(changed_steps),
        "model_input_chars": len(compact_json(model_input)),
        "local_answer_chars": len(compact_json(local_answer)),
        "model_input_sha256": payload_hash(model_input),
        "local_answer_sha256": payload_hash(local_answer),
        "raw_oracle_preserved": True,
        "runtime_state_excluded_from_model_input": True,
    }
    write_json(output_dir / "manifest.json", manifest)
    return {
        "model_input": model_input,
        "local_answer": local_answer,
        "manifest": manifest,
        "raw_oracle": raw_oracle,
    }


def prepare_task(
    task_dir,
    output_root,
    function_name=None,
    input_file="code_inputs.txt",
    limit=None,
    input_index=None,
    max_events=10000,
    max_trace_bytes=None,
):
    task_dir = Path(task_dir)
    output_root = Path(output_root)
    source_path = task_dir / "code.py"
    source = source_path.read_text(encoding="utf-8")
    function_name = function_name or resolve_task_function(task_dir)
    inputs = [
        line.strip()
        for line in (task_dir / input_file).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    indexed_inputs = list(enumerate(inputs, start=1))
    if input_index is not None:
        indexed_inputs = [row for row in indexed_inputs if row[0] == input_index]
        if not indexed_inputs:
            raise IndexError(f"input index out of range: {input_index}")
    elif limit is not None:
        indexed_inputs = indexed_inputs[:limit]

    records = []
    for index, input_text in indexed_inputs:
        input_id = f"input_{index}"
        case_dir = output_root / task_dir.name / input_id
        try:
            result = prepare_local_case(
                source=source,
                function_name=function_name,
                input_text=input_text,
                task_id=task_dir.name,
                input_id=input_id,
                output_dir=case_dir,
                source_path=source_path,
                max_events=max_events,
                max_trace_bytes=max_trace_bytes,
            )
            records.append({
                "task_id": task_dir.name,
                "input_id": input_id,
                "input": input_text,
                "status": "success",
                "event_count": result["manifest"]["event_count"],
                "change_count": result["manifest"]["change_count"],
                "case_dir": str(case_dir.resolve()),
            })
        except Exception as exc:
            records.append({
                "task_id": task_dir.name,
                "input_id": input_id,
                "input": input_text,
                "status": "failed",
                "error_type": type(exc).__name__,
                "reason": str(exc),
            })

    successes = [record for record in records if record["status"] == "success"]
    summary = {
        "schema_version": SCHEMA_VERSION,
        "task_id": task_dir.name,
        "function": function_name,
        "case_count": len(records),
        "success_count": len(successes),
        "failure_count": len(records) - len(successes),
        "event_count": sum(record["event_count"] for record in successes),
        "change_count": sum(record["change_count"] for record in successes),
        "records": records,
    }
    output_root.mkdir(parents=True, exist_ok=True)
    write_json(output_root / "summary.json", summary)
    return summary


def main():
    parser = argparse.ArgumentParser(
        description="Build a local full-block-trace and sparse-state-change oracle without calling an LLM."
    )
    parser.add_argument("--task-dir", required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--function")
    parser.add_argument("--input-file", default="code_inputs.txt")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--input-index", type=int)
    parser.add_argument("--max-events", type=int, default=10000)
    parser.add_argument("--max-trace-bytes", type=int)
    args = parser.parse_args()
    if args.limit is not None and args.input_index is not None:
        parser.error("--limit and --input-index cannot be used together")
    summary = prepare_task(
        task_dir=args.task_dir,
        output_root=args.output_root,
        function_name=args.function,
        input_file=args.input_file,
        limit=args.limit,
        input_index=args.input_index,
        max_events=args.max_events,
        max_trace_bytes=args.max_trace_bytes,
    )
    print(json.dumps(summary, ensure_ascii=False))
    if summary["failure_count"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
