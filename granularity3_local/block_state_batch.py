import argparse
import ast
import json
from collections import defaultdict
from pathlib import Path

from granularity3_local.block_state_local import compact_json
from granularity3_local.oracle import write_json, write_jsonl


BATCH_SCHEMA_VERSION = "g3-block-state-batch-v1"
SYSTEM_PROMPT = """You predict the concrete execution of Python function calls.

Input fields:
- fn: target function signature.
- blocks: target-function blocks. Each row is [block_id, source, outgoing_edges].
- outgoing_edges: [edge_type, target_block] rows; null target means termination.
- ctx: optional helper functions, imports, or constants. Treat helper calls atomically; never add helper-internal blocks to the trace.
- cases: calls to the same function, each with id and positional args.

For every case return:
- block_trace: the complete dynamic sequence of provided block IDs. Preserve all repetitions. Include every if/while test, every for-header occurrence including its final loop-exit occurrence, and the return block.
- changes: only locals created, changed, or deleted by a block. Each row is [step, variable, before, after], where step is the zero-based index into block_trace. Sort by step and then variable. Omit unchanged variables and steps.

Value encoding:
- undefined local: {"$u":1}
- Python list: JSON array
- tuple: {"$t":[...]}
- dict: {"$d":[[key,value],...]}
- set: {"$s":[...]}

Return exactly one JSON object:
{"results":[{"id":"input_1","block_trace":["B001"],"changes":[[0,"x",{"$u":1},0]]}]}

Return every requested id exactly once and in input order. Return JSON only. No explanation, Markdown, line_trace, probes, next_block, occurrence, snapshots, or return_value."""

SINGLE_CASE_SYSTEM_PROMPT = """You predict the concrete execution of one Python function call.

Input fields:
- fn: target function signature.
- blocks: target-function blocks. Each row is [block_id, source, outgoing_edges].
- outgoing_edges: [edge_type, target_block] rows; null target means termination.
- ctx: optional helper functions, imports, or constants. Treat helper calls atomically; never add helper-internal blocks to the trace.
- cases: exactly one call, with id and positional args.

Return exactly the same two fields as the local_answer.json file for this case:
- block_trace: the complete dynamic sequence of provided block IDs. Preserve all repetitions. Include every if/while test, every for-header occurrence including its final loop-exit occurrence, and the return block.
- changes: only locals created, changed, or deleted by a block. Each row is [step, variable, before, after], where step is the zero-based index into block_trace. Sort by step and then variable. Omit unchanged variables and steps.

Value encoding:
- undefined local: {"$u":1}
- Python list: JSON array
- tuple: {"$t":[...]}
- dict: {"$d":[[key,value],...]}
- set: {"$s":[...]}

Return exactly one JSON object with no wrapper and no case id:
{"block_trace":["B001"],"changes":[[0,"x",{"$u":1},0]]}

Return JSON only. No results, id, explanation, Markdown, line_trace, probes, next_block, occurrence, snapshots, or return_value."""

FLAT_RUN_SYSTEM_PROMPT = """You predict the concrete execution of one Python function call.

Input fields:
- fn: target function signature.
- blocks: target-function blocks. Each row is [block_id, source, outgoing_edges].
- outgoing_edges: [edge_type, target_block] rows; null target means termination.
- ctx: optional helper functions, imports, or constants. Treat helper calls atomically.
- cases: exactly one call, with id and positional args.

Return exactly two fields: block_trace and changes.

block_trace is a flat list of [path, repeat_count] rows:
- path is one or more provided block IDs joined by >, for example B002>B003.
- repeat_count is a positive integer.
- A non-loop block is [block_id,1].
- One loop iteration starts at the loop header and stops immediately before the next execution of that same header.
- Merge only adjacent loop iterations whose complete paths are identical.
- The final loop test that takes the loop-exit edge is a separate [header,1] row.
- Preserve execution order. Do not merge non-adjacent or different iteration paths.

changes contains only locals created, changed, or deleted within each block_trace row.
Each row is [run_index, variable, before, after], where run_index is the zero-based index into block_trace.
For a repeated path run, before is the value before the first change in the run and after is the value after the last change in the run.
Sort changes by run_index and then variable. Omit variables that never change in the run.

Value encoding:
- undefined local: {"$u":1}
- Python list: JSON array
- tuple: {"$t":[...]}
- dict: {"$d":[[key,value],...]}
- set: {"$s":[...]}

Example for B001 followed by three identical B002>B003 loop iterations, a final B002 loop-exit test, and B004:
{"block_trace":[["B001",1],["B002>B003",3],["B002",1],["B004",1]],"changes":[[0,"i",{"$u":1},0],[1,"i",0,3]]}

Return exactly one JSON object in that form. Return JSON only. No results, id, explanation, Markdown, line_trace, probes, next_block, occurrence, snapshots, or return_value."""


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def read_jsonl(path):
    path = Path(path)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def task_sort_key(task_id):
    try:
        return int(task_id.rsplit("_", 1)[1])
    except (IndexError, ValueError):
        return task_id


def input_sort_key(input_id):
    try:
        return int(input_id.rsplit("_", 1)[1])
    except (IndexError, ValueError):
        return input_id


def _loaded_names(node):
    return {item.id for item in ast.walk(node) if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Load)}


def _assigned_names(node):
    names = set()
    targets = []
    if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    for target in targets:
        names.update(item.id for item in ast.walk(target) if isinstance(item, ast.Name))
    return names


def _imported_names(node):
    if isinstance(node, ast.Import):
        return {alias.asname or alias.name.split(".", 1)[0] for alias in node.names}
    if isinstance(node, ast.ImportFrom):
        return {alias.asname or alias.name for alias in node.names}
    return set()


def _source_of(source, node):
    return ast.get_source_segment(source, node) or ast.unparse(node)


def extract_static_context(source, function_name):
    """Return only module context transitively referenced by the target function."""
    tree = ast.parse(source)
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    target = functions.get(function_name)
    if target is None:
        raise ValueError(f"function not found: {function_name}")

    selected = set()
    used_names = _loaded_names(target)
    frontier = [target]
    while frontier:
        current = frontier.pop()
        for call in (item for item in ast.walk(current) if isinstance(item, ast.Call)):
            if isinstance(call.func, ast.Name):
                helper = functions.get(call.func.id)
                if helper is not None and helper is not target and id(helper) not in selected:
                    selected.add(id(helper))
                    frontier.append(helper)
                    used_names.update(_loaded_names(helper))

    classes = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.ClassDef)
    }
    for name in sorted(used_names):
        node = classes.get(name)
        if node is not None:
            selected.add(id(node))
            used_names.update(_loaded_names(node))

    assignments = [
        node
        for node in tree.body
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign))
    ]
    changed = True
    while changed:
        changed = False
        for node in assignments:
            if id(node) in selected:
                continue
            if _assigned_names(node) & used_names:
                selected.add(id(node))
                used_names.update(_loaded_names(node))
                changed = True

    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = _imported_names(node)
            if "*" in names or names & used_names:
                selected.add(id(node))

    nodes = [node for node in tree.body if id(node) in selected]
    nodes.sort(key=lambda node: getattr(node, "lineno", 0))
    return "\n\n".join(_source_of(source, node) for node in nodes)


def latest_records(records):
    latest = {}
    for row in records:
        latest[row["case_key"]] = row
    return list(latest.values())


def pack_cases(cases, max_cases, max_batch_answer_chars):
    if max_cases < 1:
        raise ValueError("max_cases must be positive")
    batches = []
    current = []
    current_chars = 0
    for case in cases:
        answer_chars = case["answer_chars"]
        if current and (
            len(current) >= max_cases
            or current_chars + answer_chars > max_batch_answer_chars
        ):
            batches.append(current)
            current = []
            current_chars = 0
        current.append(case)
        current_chars += answer_chars
    if current:
        batches.append(current)
    return batches


def build_dataset_batches(
    dataset_root,
    local_output_root,
    output_dir,
    max_cases=10,
    max_events=500,
    max_answer_chars=10000,
    max_batch_answer_chars=50000,
    one_case_per_request=False,
):
    if one_case_per_request:
        max_cases = 1
    dataset_root = Path(dataset_root)
    local_output_root = Path(local_output_root)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    records = latest_records(read_jsonl(local_output_root / "case_records.jsonl"))
    grouped = defaultdict(list)
    excluded = []
    for row in records:
        if row.get("status") not in {"success", "cached"}:
            excluded.append({
                "case_key": row["case_key"],
                "reason": row.get("error_type") or row.get("status"),
            })
            continue
        if row["event_count"] > max_events:
            excluded.append({
                "case_key": row["case_key"],
                "reason": "event_limit",
                "value": row["event_count"],
                "limit": max_events,
            })
            continue
        if row["local_answer_chars"] > max_answer_chars:
            excluded.append({
                "case_key": row["case_key"],
                "reason": "answer_size_limit",
                "value": row["local_answer_chars"],
                "limit": max_answer_chars,
            })
            continue
        grouped[row["task_id"]].append(row)

    model_batches = []
    answer_batches = []
    batch_manifests = []
    old_request_chars = 0
    new_request_chars = 0
    context_tasks = []
    for task_id in sorted(grouped, key=task_sort_key):
        task_rows = sorted(grouped[task_id], key=lambda row: input_sort_key(row["input_id"]))
        source = (dataset_root / task_id / "code.py").read_text(encoding="utf-8")
        function_name = task_rows[0]["function"]
        context = extract_static_context(source, function_name)
        if context:
            context_tasks.append(task_id)

        prepared = []
        shared_fn = None
        shared_blocks = None
        for row in task_rows:
            case_dir = local_output_root / "cases" / task_id / row["input_id"]
            model_input = read_json(case_dir / "model_input.json")
            local_answer = read_json(case_dir / "local_answer.json")
            if shared_fn is None:
                shared_fn = model_input["fn"]
                shared_blocks = model_input["blocks"]
            elif model_input["fn"] != shared_fn or model_input["blocks"] != shared_blocks:
                raise ValueError(f"static model input differs within task: {task_id}")
            old_request_chars += row["model_input_chars"]
            prepared.append({
                "id": row["input_id"],
                "args": model_input["args"],
                "answer": local_answer,
                "answer_chars": row["local_answer_chars"],
            })

        for batch_index, packed in enumerate(
            pack_cases(prepared, max_cases=max_cases, max_batch_answer_chars=max_batch_answer_chars),
            start=1,
        ):
            batch_id = f"{task_id}/batch_{batch_index}"
            request = {
                "fn": shared_fn,
                "blocks": shared_blocks,
                "cases": [{"id": case["id"], "args": case["args"]} for case in packed],
            }
            if context:
                request["ctx"] = context
            answers = {
                "batch_id": batch_id,
                "results": [
                    {"id": case["id"], **case["answer"]}
                    for case in packed
                ],
            }
            request_chars = len(compact_json(request))
            new_request_chars += request_chars
            model_record = {"batch_id": batch_id, "request": request}
            if one_case_per_request:
                model_record["response_format"] = "single_case"
            model_batches.append(model_record)
            answer_record = answers
            if one_case_per_request:
                answer_record["response_format"] = "single_case"
            answer_batches.append(answer_record)
            batch_manifests.append({
                "batch_id": batch_id,
                "task_id": task_id,
                "case_ids": [case["id"] for case in packed],
                "case_count": len(packed),
                "request_chars": request_chars,
                "expected_answer_chars": sum(case["answer_chars"] for case in packed),
                "context_chars": len(context),
            })

    write_jsonl(output_dir / "model_batches.jsonl", model_batches)
    write_jsonl(output_dir / "local_answer_batches.jsonl", answer_batches)
    write_jsonl(output_dir / "batch_manifest.jsonl", batch_manifests)
    write_jsonl(output_dir / "excluded_cases.jsonl", excluded)

    eligible_case_count = sum(row["case_count"] for row in batch_manifests)
    batch_count = len(model_batches)
    old_prompt_chars = old_request_chars + len(SYSTEM_PROMPT) * eligible_case_count
    new_prompt_chars = new_request_chars + len(SYSTEM_PROMPT) * batch_count
    summary = {
        "schema_version": BATCH_SCHEMA_VERSION,
        "request_mode": "per_case" if max_cases == 1 else "task_batch",
        "dataset_root": str(dataset_root.resolve()),
        "local_output_root": str(local_output_root.resolve()),
        "max_cases_per_batch": max_cases,
        "max_events_per_case": max_events,
        "max_answer_chars_per_case": max_answer_chars,
        "max_expected_answer_chars_per_batch": max_batch_answer_chars,
        "eligible_task_count": len(grouped),
        "eligible_case_count": eligible_case_count,
        "excluded_case_count": len(excluded),
        "batch_count": batch_count,
        "mean_cases_per_batch": eligible_case_count / batch_count if batch_count else 0,
        "context_task_count": len(context_tasks),
        "context_tasks": context_tasks,
        "unbatched_request_chars": old_request_chars,
        "batched_request_chars": new_request_chars,
        "request_char_reduction": 1 - new_request_chars / old_request_chars if old_request_chars else 0,
        "unbatched_prompt_chars": old_prompt_chars,
        "batched_prompt_chars": new_prompt_chars,
        "prompt_char_reduction": 1 - new_prompt_chars / old_prompt_chars if old_prompt_chars else 0,
        "unbatched_request_count": eligible_case_count,
        "batched_request_count": batch_count,
        "request_count_reduction": 1 - batch_count / eligible_case_count if eligible_case_count else 0,
        "rough_batched_prompt_tokens": round(new_prompt_chars / 4),
        "answer_isolation": True,
    }
    write_json(output_dir / "summary.json", summary)
    (output_dir / "system_prompt.txt").write_text(SYSTEM_PROMPT + "\n", encoding="utf-8")
    return {
        "summary": summary,
        "model_batches": model_batches,
        "answer_batches": answer_batches,
        "manifests": batch_manifests,
        "excluded": excluded,
    }


def main():
    parser = argparse.ArgumentParser(description="Prepare block/state cases for model requests.")
    parser.add_argument("--dataset-root", required=True)
    parser.add_argument("--local-output-root", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--max-cases", type=int, default=10)
    parser.add_argument("--max-events", type=int, default=500)
    parser.add_argument("--max-answer-chars", type=int, default=10000)
    parser.add_argument("--max-batch-answer-chars", type=int, default=50000)
    parser.add_argument(
        "--one-case-per-request",
        action="store_true",
        help="Write one model request and one Oracle row for each task input.",
    )
    args = parser.parse_args()
    result = build_dataset_batches(
        dataset_root=args.dataset_root,
        local_output_root=args.local_output_root,
        output_dir=args.output_dir,
        max_cases=args.max_cases,
        max_events=args.max_events,
        max_answer_chars=args.max_answer_chars,
        max_batch_answer_chars=args.max_batch_answer_chars,
        one_case_per_request=args.one_case_per_request,
    )
    print(json.dumps(result["summary"], ensure_ascii=False))


if __name__ == "__main__":
    main()
