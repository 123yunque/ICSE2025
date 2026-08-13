import argparse
import ast
import json
import os
import shutil
import time
from pathlib import Path

from granularity3_local.api_smoke import exception_chain, parse_json_response
from granularity3_local.compact import select_probe_indices
from granularity3_local.oracle import build_oracle_case, parse_input, write_json
from granularity3_local.preflight import resolve_task_function
from granularity3_local.probes import build_probe_dataset
from granularity3_local.state import canonicalize


SYSTEM_PROMPT = """You are evaluating the concrete execution of one Python function.

The user provides a numbered target function, concrete positional input, static basic blocks, statically possible CFG edges, and selected runtime probes.

Input fields:
- function: target function name.
- input: concrete positional arguments.
- numbered_code: [source line number, source text] pairs for the target function.
- blocks: static basic blocks. id is unique in this function; line is its source line; source is its code.
- cfg_edges: all statically possible transitions, not the actual runtime path.
- probes: selected runtime checkpoints.
- current_block: block about to execute.
- occurrence: 1-based execution count of that block in this run.
- pre_state: target-function locals immediately before that block executes.

Output fields:
- line_trace: exact source-line sequence executed by the target function. Preserve loop repetitions. Exclude the function definition line and all helper/library/test/instrumentation lines.
- next_block: block entered immediately after the probe's current block; null if the target function terminates.
- state_delta: only locals created, changed, or deleted by the current block, each as {\"before\":...,\"after\":...}. Do not copy unchanged locals.
- return_value: final value returned by the target function.

Represent an undefined local exactly as {\"$undefined\":true}. Never use null or a string for undefined.

Return exactly one JSON object:
{\"line_trace\":[1,2],\"probes\":[{\"id\":\"p1\",\"next_block\":\"B001\",\"state_delta\":{}}],\"return_value\":null}

Return JSON only. No explanation, comments, analysis, or Markdown fences."""


DEFAULT_TASKS = [
    "task_223",  # simple branch
    "task_18",   # for loop
    "task_9",    # for + branch
    "task_235",  # while loop
    "task_160",  # while + branch
    "task_109",  # repeated state updates
    "task_126",  # for + branch
    "task_20",   # longer while + branch
    "task_734",  # while state accumulation
    "task_296",  # nested loop/branch
]


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def read_jsonl(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def positional_input(text):
    value = parse_input(text)
    args = list(value) if isinstance(value, tuple) else [value]
    return canonicalize(args)


def numbered_function(source, function_name):
    tree = ast.parse(source)
    target = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == function_name)
    lines = source.splitlines()
    return [[number, lines[number - 1]] for number in range(target.lineno, target.end_lineno + 1)]


def evenly_limit(indices, limit):
    if limit is None or len(indices) <= limit:
        return indices
    if limit == 1:
        return [indices[0]]
    positions = [round(i * (len(indices) - 1) / (limit - 1)) for i in range(limit)]
    return [indices[position] for position in positions]


def build_joint_case(source, model_case, model_inputs, answers, line_trace, result, max_probes=8):
    selected = select_probe_indices(model_case, model_inputs, answers, max_occurrences=3)
    selected = evenly_limit(selected, max_probes)
    blocks = []
    for block_id, block in sorted(model_case["blocks"].items()):
        blocks.append({"id": block_id, "line": block["source_span"][0], "source": block["source"]})
    payload_probes = []
    expected_probes = []
    probe_map = []
    for short_index, source_index in enumerate(selected, start=1):
        probe = model_inputs[source_index]
        answer = answers[source_index]
        short_id = f"p{short_index}"
        payload_probes.append({
            "id": short_id,
            "current_block": probe["current_block"],
            "occurrence": int(probe["target_event"].rsplit("#", 1)[-1]),
            "pre_state": probe["state_before"],
        })
        expected = {
            "id": short_id,
            "next_block": answer["next"],
            "state_delta": answer["delta"],
        }
        expected_probes.append(expected)
        probe_map.append({"id": short_id, "probe_id": probe["probe_id"]})
    payload = {
        "function": model_case["function"],
        "input": positional_input(model_case["input"]),
        "numbered_code": numbered_function(source, model_case["function"]),
        "blocks": blocks,
        "cfg_edges": [
            {"from": edge["from"], "to": edge["to"], "type": edge["edge_type"]}
            for edge in model_case["cfg_edges"]
        ],
        "probes": payload_probes,
    }
    expected = {
        "line_trace": line_trace["source_lines"],
        "probes": expected_probes,
        "return_value": result,
    }
    return payload, expected, probe_map


def longest_common_prefix(left, right):
    count = 0
    for first, second in zip(left, right):
        if first != second:
            break
        count += 1
    return count


def edit_distance(left, right):
    previous = list(range(len(right) + 1))
    for i, first in enumerate(left, start=1):
        current = [i]
        for j, second in enumerate(right, start=1):
            current.append(min(current[-1] + 1, previous[j] + 1, previous[j - 1] + (first != second)))
        previous = current
    return previous[-1]


def normalize_delta(delta):
    return {
        name: change
        for name, change in delta.items()
        if not isinstance(change, dict) or change.get("before") != change.get("after")
    }


def score_prediction(prediction, expected):
    predicted_trace = prediction.get("line_trace", [])
    expected_trace = expected["line_trace"]
    predicted_probes = {row.get("id"): row for row in prediction.get("probes", []) if isinstance(row, dict)}
    probe_rows = []
    for answer in expected["probes"]:
        predicted = predicted_probes.get(answer["id"], {})
        next_ok = predicted.get("next_block") == answer["next_block"]
        delta_ok = predicted.get("state_delta") == answer["state_delta"]
        semantic_delta_ok = normalize_delta(predicted.get("state_delta", {})) == normalize_delta(answer["state_delta"])
        probe_rows.append({
            "id": answer["id"],
            "next_correct": next_ok,
            "delta_correct": delta_ok,
            "delta_semantic_correct": semantic_delta_ok,
            "exact": next_ok and delta_ok,
            "semantic_exact": next_ok and semantic_delta_ok,
        })
    line_exact = predicted_trace == expected_trace
    return_correct = prediction.get("return_value") == expected["return_value"]
    all_probes_exact = all(row["exact"] for row in probe_rows)
    all_probes_semantic_exact = all(row["semantic_exact"] for row in probe_rows)
    return {
        "line_trace_exact": line_exact,
        "line_trace_expected_length": len(expected_trace),
        "line_trace_predicted_length": len(predicted_trace),
        "line_trace_longest_correct_prefix": longest_common_prefix(predicted_trace, expected_trace),
        "line_trace_edit_distance": edit_distance(predicted_trace, expected_trace),
        "return_correct": return_correct,
        "probe_count": len(probe_rows),
        "next_correct_count": sum(row["next_correct"] for row in probe_rows),
        "delta_correct_count": sum(row["delta_correct"] for row in probe_rows),
        "delta_semantic_correct_count": sum(row["delta_semantic_correct"] for row in probe_rows),
        "probe_exact_count": sum(row["exact"] for row in probe_rows),
        "probe_semantic_exact_count": sum(row["semantic_exact"] for row in probe_rows),
        "probe_rows": probe_rows,
        "joint_exact": line_exact and return_correct and all_probes_exact,
        "joint_semantic_exact": line_exact and return_correct and all_probes_semantic_exact,
    }


def call_model(client, model, payload, timeout):
    started = time.perf_counter()
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)},
        ],
        temperature=0,
        timeout=timeout,
    )
    content = response.choices[0].message.content or ""
    prediction = parse_json_response(content)
    if not isinstance(prediction, dict):
        raise ValueError("model response must be one JSON object")
    usage = response.usage
    return prediction, content, {
        "elapsed_seconds": time.perf_counter() - started,
        "response_model": getattr(response, "model", model),
        "prompt_tokens": getattr(usage, "prompt_tokens", None),
        "completion_tokens": getattr(usage, "completion_tokens", None),
        "total_tokens": getattr(usage, "total_tokens", None),
    }


def summarize(records):
    successful = [row for row in records if row["status"] == "success"]
    probe_count = sum(row["score"]["probe_count"] for row in successful)
    return {
        "task_count": len(records),
        "successful_task_count": len(successful),
        "failed_task_count": len(records) - len(successful),
        "line_trace_exact_count": sum(row["score"]["line_trace_exact"] for row in successful),
        "return_correct_count": sum(row["score"]["return_correct"] for row in successful),
        "joint_exact_count": sum(row["score"]["joint_exact"] for row in successful),
        "joint_semantic_exact_count": sum(row["score"]["joint_semantic_exact"] for row in successful),
        "probe_count": probe_count,
        "next_correct_count": sum(row["score"]["next_correct_count"] for row in successful),
        "delta_correct_count": sum(row["score"]["delta_correct_count"] for row in successful),
        "delta_semantic_correct_count": sum(row["score"]["delta_semantic_correct_count"] for row in successful),
        "probe_exact_count": sum(row["score"]["probe_exact_count"] for row in successful),
        "probe_semantic_exact_count": sum(row["score"]["probe_semantic_exact_count"] for row in successful),
        "next_accuracy": sum(row["score"]["next_correct_count"] for row in successful) / probe_count if probe_count else None,
        "delta_accuracy": sum(row["score"]["delta_correct_count"] for row in successful) / probe_count if probe_count else None,
        "delta_semantic_accuracy": sum(row["score"]["delta_semantic_correct_count"] for row in successful) / probe_count if probe_count else None,
        "probe_exact_accuracy": sum(row["score"]["probe_exact_count"] for row in successful) / probe_count if probe_count else None,
        "probe_semantic_exact_accuracy": sum(row["score"]["probe_semantic_exact_count"] for row in successful) / probe_count if probe_count else None,
        "prompt_tokens": sum(row["api"]["prompt_tokens"] or 0 for row in successful),
        "completion_tokens": sum(row["api"]["completion_tokens"] or 0 for row in successful),
        "total_tokens": sum(row["api"]["total_tokens"] or 0 for row in successful),
        "elapsed_seconds": sum(row["api"]["elapsed_seconds"] for row in successful),
        "records": records,
    }


def main():
    parser = argparse.ArgumentParser(description="Run a 10-task joint granularity-3 API pilot.")
    parser.add_argument("--dataset-root", required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--tasks", nargs="*", default=DEFAULT_TASKS)
    parser.add_argument("--input-index", type=int, default=1)
    parser.add_argument("--max-probes", type=int, default=8)
    parser.add_argument("--model", default=os.getenv("YUNWU_MODEL", ""))
    parser.add_argument("--base-url", default=os.getenv("YUNWU_API_BASE_URL", ""))
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument("--api-only", action="store_true", help="Reuse existing model_request.json and local_answer.json.")
    parser.add_argument("--summarize-only", action="store_true", help="Re-score saved predictions without API calls.")
    args = parser.parse_args()
    if args.summarize_only:
        output_root = Path(args.output_root)
        api_metrics_path = output_root / "api_metrics.json"
        api_metrics = read_json(api_metrics_path) if api_metrics_path.exists() else {}
        records = []
        for task_id in args.tasks:
            case_dir = output_root / task_id / f"input_{args.input_index}"
            try:
                payload = read_json(case_dir / "model_request.json")
                prediction = read_json(case_dir / "model_prediction.json")
                expected = read_json(case_dir / "local_answer.json")
                score = score_prediction(prediction, expected)
                write_json(case_dir / "score.json", score)
                records.append({
                    "task_id": task_id,
                    "function": payload["function"],
                    "status": "success",
                    "score": score,
                    "api": api_metrics.get(task_id, {}),
                })
            except Exception as exc:
                records.append({"task_id": task_id, "status": "failed", "error_type": type(exc).__name__, "reason": str(exc)})
        summary = summarize(records)
        summary["model"] = args.model
        summary["input_index"] = args.input_index
        summary["max_probes_per_task"] = args.max_probes
        write_json(output_root / "summary.json", summary)
        print(json.dumps({key: value for key, value in summary.items() if key != "records"}, ensure_ascii=False))
        return

    api_key = os.getenv("YUNWU_API_KEY", "").strip()
    if not api_key or not args.model or not args.base_url:
        raise SystemExit("YUNWU_API_KEY, YUNWU_MODEL, and YUNWU_API_BASE_URL are required")
    from openai import OpenAI

    client = OpenAI(api_key=api_key, base_url=args.base_url, max_retries=0)
    dataset_root = Path(args.dataset_root)
    output_root = Path(args.output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    records = []
    try:
        for task_id in args.tasks:
            task_dir = dataset_root / task_id
            case_dir = output_root / task_id / f"input_{args.input_index}"
            case_dir.mkdir(parents=True, exist_ok=True)
            try:
                if args.api_only:
                    payload = read_json(case_dir / "model_request.json")
                    expected = read_json(case_dir / "local_answer.json")
                    function = payload["function"]
                    input_text = json.dumps(payload["input"], ensure_ascii=False)
                else:
                    source_path = task_dir / "code.py"
                    source = source_path.read_text(encoding="utf-8")
                    function = resolve_task_function(task_dir)
                    inputs = [line.strip() for line in (task_dir / "code_inputs.txt").read_text(encoding="utf-8").splitlines() if line.strip()]
                    input_text = inputs[args.input_index - 1]
                    shutil.copy2(source_path, case_dir / "code.py")
                    oracle = build_oracle_case(source, function, input_text, task_id, "original", f"input_{args.input_index}", case_dir / "oracle", source_path=case_dir / "code.py")
                    probes = build_probe_dataset(case_dir / "oracle", case_dir / "probes")
                    payload, expected, probe_map = build_joint_case(
                        source,
                        probes["case"],
                        probes["model_inputs"],
                        probes["answers"],
                        oracle["line_trace"],
                        oracle["case"]["result"],
                        max_probes=args.max_probes,
                    )
                    write_json(case_dir / "model_request.json", payload)
                    write_json(case_dir / "local_answer.json", expected)
                    write_json(case_dir / "probe_map.json", probe_map)
                prediction, raw, api = call_model(client, args.model, payload, args.timeout)
                write_json(case_dir / "api.json", api)
                write_json(case_dir / "model_prediction.json", prediction)
                (case_dir / "raw_response.txt").write_text(raw, encoding="utf-8")
                score = score_prediction(prediction, expected)
                write_json(case_dir / "score.json", score)
                record = {"task_id": task_id, "function": function, "input": input_text, "status": "success", "score": score, "api": api}
            except Exception as exc:
                record = {"task_id": task_id, "status": "failed", "error_type": type(exc).__name__, "reason": str(exc), "exception_chain": exception_chain(exc)}
            records.append(record)
            print(json.dumps(record, ensure_ascii=False), flush=True)
    finally:
        client.close()
    summary = summarize(records)
    summary["model"] = args.model
    summary["input_index"] = args.input_index
    summary["max_probes_per_task"] = args.max_probes
    write_json(output_root / "summary.json", summary)
    print(json.dumps({key: value for key, value in summary.items() if key != "records"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
