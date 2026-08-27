import argparse
import hashlib
import json
import math
from collections import defaultdict
from pathlib import Path

from granularity3_local.block_state_batch import read_jsonl
from granularity3_local.oracle import write_json


CANARY_SCHEMA_VERSION = "g3-block-state-canary-v1"
ROLLOUT_SCHEMA_VERSION = "g3-block-state-rollout-v1"


def _percentile(values, percentile):
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def _received_attempts_by_case(rows):
    received = defaultdict(list)
    for row in rows:
        if isinstance(row, dict) and row.get("status") == "received":
            received[row.get("batch_id")].append(row)
    return received


def select_stratified_case_keys(baseline_dir, sample_size=40):
    baseline_dir = Path(baseline_dir)
    model_rows = read_jsonl(baseline_dir / "selected_model_batches.jsonl")
    attempts = read_jsonl(baseline_dir / "api_attempts.jsonl")
    scores = {
        row["case_key"]: row
        for row in read_jsonl(baseline_dir / "evaluation" / "case_scores.jsonl")
    }
    received_by_case = _received_attempts_by_case(attempts)
    ordered_keys = [row["batch_id"] for row in model_rows]
    task_order = []
    keys_by_task = defaultdict(list)
    for case_key in ordered_keys:
        task_id = case_key.split("/", 1)[0]
        if task_id not in keys_by_task:
            task_order.append(task_id)
        keys_by_task[task_id].append(case_key)
    if sample_size < len(task_order):
        raise ValueError("sample_size must be at least the number of baseline tasks")
    if sample_size > len(ordered_keys):
        raise ValueError("sample_size exceeds available baseline cases")

    base_quota, remainder = divmod(sample_size, len(task_order))
    quotas = {
        task_id: base_quota + (1 if index < remainder else 0)
        for index, task_id in enumerate(task_order)
    }
    shortfall = 0
    for task_id in task_order:
        if quotas[task_id] > len(keys_by_task[task_id]):
            shortfall += quotas[task_id] - len(keys_by_task[task_id])
            quotas[task_id] = len(keys_by_task[task_id])
    while shortfall:
        progressed = False
        for task_id in task_order:
            if quotas[task_id] < len(keys_by_task[task_id]):
                quotas[task_id] += 1
                shortfall -= 1
                progressed = True
                if not shortfall:
                    break
        if not progressed:
            raise ValueError("cannot allocate requested sample across tasks")

    selection = []
    for task_id in task_order:
        candidates = []
        for case_key in keys_by_task[task_id]:
            rows = received_by_case.get(case_key, [])
            latency = rows[-1].get("elapsed_seconds") if rows else float("inf")
            candidates.append((float(latency), case_key))
        candidates.sort(key=lambda item: (item[0], item[1]))
        quota = quotas[task_id]
        if quota == 1:
            positions = [len(candidates) // 2]
        else:
            positions = [
                round(index * (len(candidates) - 1) / (quota - 1))
                for index in range(quota)
            ]
        for position in positions:
            latency, case_key = candidates[position]
            score = scores.get(case_key, {})
            selection.append({
                "case_key": case_key,
                "task_id": task_id,
                "baseline_elapsed_seconds": latency,
                "baseline_expanded_block_exact": score.get("expanded_block_exact"),
                "baseline_changes_exact": score.get("changes_exact"),
            })
    if len(selection) != sample_size:
        raise AssertionError(f"selected {len(selection)} cases instead of {sample_size}")
    return selection


def write_selection(baseline_dir, output_dir, sample_size=40):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    selection = select_stratified_case_keys(baseline_dir, sample_size=sample_size)
    case_keys = [row["case_key"] for row in selection]
    (output_dir / "canary_case_keys.txt").write_text(
        "\n".join(case_keys) + "\n",
        encoding="utf-8",
    )
    (output_dir / "probe_case_key.txt").write_text(
        min(selection, key=lambda row: row["baseline_elapsed_seconds"])["case_key"] + "\n",
        encoding="utf-8",
    )
    artifact = {
        "schema_version": CANARY_SCHEMA_VERSION,
        "baseline_dir": str(Path(baseline_dir).resolve()),
        "sample_size": sample_size,
        "task_count": len({row["task_id"] for row in selection}),
        "selection": selection,
    }
    write_json(output_dir / "selection.json", artifact)
    return artifact


def _task_sort_key(task_id):
    suffix = task_id.rsplit("_", 1)[-1]
    return (0, int(suffix)) if suffix.isdigit() else (1, task_id)


def _request_loop_header_count(request):
    count = 0
    for block in request.get("blocks", []):
        source = str(block[1]).lstrip() if len(block) > 1 else ""
        if source.startswith(("for ", "while ")):
            count += 1
    return count


def _allocate_proportional_quotas(counts, sample_size):
    total = sum(counts.values())
    raw = {key: sample_size * count / total for key, count in counts.items()}
    quotas = {key: min(counts[key], int(math.floor(value))) for key, value in raw.items()}
    remaining = sample_size - sum(quotas.values())
    order = sorted(
        counts,
        key=lambda key: (raw[key] - math.floor(raw[key]), counts[key], key),
        reverse=True,
    )
    while remaining:
        progressed = False
        for key in order:
            if quotas[key] < counts[key]:
                quotas[key] += 1
                remaining -= 1
                progressed = True
                if not remaining:
                    break
        if not progressed:
            raise ValueError("cannot allocate rollout sample across complexity strata")
    return quotas


def select_full_rollout_cases(model_batches, sample_size=300):
    tasks = {}
    for row in model_batches:
        task_id = row["batch_id"].split("/", 1)[0]
        task = tasks.setdefault(task_id, {"cases": [], "loop_headers": 0})
        task["cases"].extend(row["request"].get("cases", []))
        task["loop_headers"] = max(
            task["loop_headers"],
            _request_loop_header_count(row["request"]),
        )
    if sample_size < 1 or sample_size > len(tasks):
        raise ValueError(
            f"sample_size must be between 1 and the {len(tasks)} available tasks"
        )

    strata = defaultdict(list)
    for task_id, task in tasks.items():
        loop_headers = task["loop_headers"]
        complexity = "0_loops" if loop_headers == 0 else (
            "1_loop" if loop_headers == 1 else "2plus_loops"
        )
        strata[complexity].append(task_id)
    for task_ids in strata.values():
        task_ids.sort(key=_task_sort_key)
    quotas = _allocate_proportional_quotas(
        {key: len(value) for key, value in strata.items()},
        sample_size,
    )

    selected_tasks = []
    for complexity in sorted(strata):
        candidates = strata[complexity]
        quota = quotas[complexity]
        if quota == 1:
            positions = [len(candidates) // 2]
        else:
            positions = [
                round(index * (len(candidates) - 1) / (quota - 1))
                for index in range(quota)
            ]
        selected_tasks.extend((candidates[position], complexity) for position in positions)
    selected_tasks.sort(key=lambda item: _task_sort_key(item[0]))

    selection = []
    for task_id, complexity in selected_tasks:
        cases = tasks[task_id]["cases"]
        if not cases:
            raise ValueError(f"{task_id} has no cases")
        digest = hashlib.sha256(task_id.encode("utf-8")).digest()
        case = cases[int.from_bytes(digest[:4], "big") % len(cases)]
        selection.append({
            "case_key": f"{task_id}/{case['id']}",
            "task_id": task_id,
            "input_id": case["id"],
            "loop_headers": tasks[task_id]["loop_headers"],
            "complexity": complexity,
        })
    if len(selection) != sample_size or len({row["task_id"] for row in selection}) != sample_size:
        raise AssertionError("rollout selection must contain one case per unique task")
    return selection, len(tasks), {key: len(value) for key, value in strata.items()}


def write_full_rollout_selection(model_batches_path, output_dir, sample_size=300):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    selection, full_task_count, population_strata = select_full_rollout_cases(
        read_jsonl(model_batches_path),
        sample_size=sample_size,
    )
    case_keys = [row["case_key"] for row in selection]
    (output_dir / "stage_case_keys.txt").write_text(
        "\n".join(case_keys) + "\n",
        encoding="utf-8",
    )
    artifact = {
        "schema_version": ROLLOUT_SCHEMA_VERSION,
        "model_batches": str(Path(model_batches_path).resolve()),
        "full_task_count": full_task_count,
        "sample_size": sample_size,
        "sample_task_count": len(selection),
        "population_complexity_task_counts": population_strata,
        "sample_complexity_task_counts": dict(sorted(
            (key, sum(row["complexity"] == key for row in selection))
            for key in population_strata
        )),
        "ordered_case_keys_sha256": hashlib.sha256(
            json.dumps(case_keys, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        "selection": selection,
    }
    write_json(output_dir / "selection.json", artifact)
    return artifact


def _summarize_run(directory, case_keys):
    directory = Path(directory)
    key_set = set(case_keys)
    attempts = [
        row
        for row in read_jsonl(directory / "api_attempts.jsonl")
        if isinstance(row, dict) and row.get("batch_id") in key_set
    ]
    scores = {
        row["case_key"]: row
        for row in read_jsonl(directory / "evaluation" / "case_scores.jsonl")
        if row.get("case_key") in key_set
    }
    received_by_case = _received_attempts_by_case(attempts)
    final_received = [rows[-1] for rows in received_by_case.values() if rows]
    case_wall_seconds = []
    for case_key in case_keys:
        case_rows = [row for row in attempts if row.get("batch_id") == case_key]
        if case_rows:
            case_wall_seconds.append(sum(float(row.get("elapsed_seconds", 0)) for row in case_rows))
    expected = len(case_keys)
    expanded_count = sum(
        bool(scores.get(case_key, {}).get("expanded_block_exact"))
        for case_key in case_keys
    )
    changes_count = sum(
        bool(scores.get(case_key, {}).get("changes_exact"))
        for case_key in case_keys
    )
    canonical_joint_count = sum(
        bool(scores.get(case_key, {}).get("canonical_joint_exact"))
        for case_key in case_keys
    )
    unsupported_parameter_errors = [
        row
        for row in attempts
        if row.get("status") == "api_error"
        and any(
            marker in str(row.get("reason", "")).lower()
            for marker in ("unknown parameter", "unsupported", "reasoning_effort", "verbosity")
        )
    ]
    return {
        "expected_case_count": expected,
        "scored_case_count": len(scores),
        "received_case_count": len(final_received),
        "expanded_block_exact_count": expanded_count,
        "expanded_block_exact_rate": expanded_count / expected if expected else None,
        "changes_exact_count": changes_count,
        "changes_exact_rate": changes_count / expected if expected else None,
        "canonical_joint_exact_count": canonical_joint_count,
        "canonical_joint_exact_rate": canonical_joint_count / expected if expected else None,
        "completion_tokens": sum(row.get("completion_tokens", 0) for row in attempts),
        "reasoning_tokens": sum(row.get("reasoning_tokens", 0) for row in attempts),
        "prompt_tokens": sum(row.get("prompt_tokens", 0) for row in attempts),
        "total_tokens": sum(row.get("total_tokens", 0) for row in attempts),
        "api_error_count": sum(row.get("status") == "api_error" for row in attempts),
        "invalid_response_count": sum(row.get("validation") == "invalid" for row in attempts),
        "unsupported_parameter_error_count": len(unsupported_parameter_errors),
        "reasoning_details_reported_count": sum(
            bool(row.get("reasoning_tokens_reported")) for row in final_received
        ),
        "response_latency_seconds": {
            "p50": _percentile(
                [row.get("elapsed_seconds", 0) for row in final_received], 0.50
            ),
            "p90": _percentile(
                [row.get("elapsed_seconds", 0) for row in final_received], 0.90
            ),
            "max": max(
                [row.get("elapsed_seconds", 0) for row in final_received],
                default=None,
            ),
        },
        "case_wall_seconds": {
            "p90": _percentile(case_wall_seconds, 0.90),
            "sum": sum(case_wall_seconds),
        },
    }


def compare_canary(
    baseline_dir,
    candidate_dir,
    case_keys,
    max_block_drop=0.05,
    min_token_reduction=0.30,
    min_p90_reduction=0.30,
):
    baseline = _summarize_run(baseline_dir, case_keys)
    candidate = _summarize_run(candidate_dir, case_keys)

    def reduction(candidate_value, baseline_value):
        if not baseline_value:
            return None
        return 1 - candidate_value / baseline_value

    block_delta = (
        candidate["expanded_block_exact_rate"]
        - baseline["expanded_block_exact_rate"]
    )
    token_reduction = reduction(
        candidate["completion_tokens"], baseline["completion_tokens"]
    )
    p90_reduction = reduction(
        candidate["response_latency_seconds"]["p90"],
        baseline["response_latency_seconds"]["p90"],
    )
    parameter_support = (
        candidate["received_case_count"] == len(case_keys)
        and candidate["unsupported_parameter_error_count"] == 0
    )
    criteria = {
        "block_accuracy_no_material_drop": block_delta >= -max_block_drop,
        "completion_tokens_materially_reduced": (
            token_reduction is not None and token_reduction >= min_token_reduction
        ),
        "p90_latency_materially_reduced": (
            p90_reduction is not None and p90_reduction >= min_p90_reduction
        ),
        "provider_accepted_parameters": parameter_support,
        "provider_execution_evidence": (
            parameter_support
            and (
                candidate["reasoning_details_reported_count"] > 0
                or (token_reduction is not None and token_reduction >= min_token_reduction)
            )
        ),
    }
    return {
        "schema_version": CANARY_SCHEMA_VERSION,
        "case_count": len(case_keys),
        "thresholds": {
            "max_block_accuracy_drop": max_block_drop,
            "min_completion_token_reduction": min_token_reduction,
            "min_p90_latency_reduction": min_p90_reduction,
        },
        "baseline": baseline,
        "candidate": candidate,
        "deltas": {
            "expanded_block_accuracy": block_delta,
            "completion_token_reduction": token_reduction,
            "p90_latency_reduction": p90_reduction,
        },
        "criteria": criteria,
        "all_criteria_pass": all(criteria.values()),
    }


def _format_percent(value):
    return "n/a" if value is None else f"{100 * value:.2f}%"


def write_comparison(baseline_dir, candidate_dir, case_keys_file, output_dir):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    case_keys = [
        line.strip()
        for line in Path(case_keys_file).read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    comparison = compare_canary(baseline_dir, candidate_dir, case_keys)
    write_json(output_dir / "comparison.json", comparison)
    baseline = comparison["baseline"]
    candidate = comparison["candidate"]
    deltas = comparison["deltas"]
    criteria_rows = "\n".join(
        f"- {name}: {'PASS' if passed else 'FAIL'}"
        for name, passed in comparison["criteria"].items()
    )
    report = f"""# Block/state latency canary comparison

Cases: {comparison['case_count']}

| Metric | Baseline | Candidate | Change |
|---|---:|---:|---:|
| Expanded Block exact | {_format_percent(baseline['expanded_block_exact_rate'])} | {_format_percent(candidate['expanded_block_exact_rate'])} | {_format_percent(deltas['expanded_block_accuracy'])} |
| Completion tokens | {baseline['completion_tokens']} | {candidate['completion_tokens']} | {_format_percent(deltas['completion_token_reduction'])} reduction |
| Response P90 seconds | {baseline['response_latency_seconds']['p90']} | {candidate['response_latency_seconds']['p90']} | {_format_percent(deltas['p90_latency_reduction'])} reduction |
| Received cases | {baseline['received_case_count']} | {candidate['received_case_count']} | - |
| API errors | {baseline['api_error_count']} | {candidate['api_error_count']} | - |

## Criteria

{criteria_rows}

Overall: {'PASS' if comparison['all_criteria_pass'] else 'FAIL'}
"""
    (output_dir / "REPORT.md").write_text(report, encoding="utf-8")
    return comparison


def check_rollout_gates(
    run_dir,
    case_keys,
    min_format_valid_rate=0.95,
    min_expanded_block_rate=0.725,
    max_p90_seconds=60.0,
    max_api_error_rate=0.02,
    max_cap_hit_rate=0.01,
):
    run_dir = Path(run_dir)
    metrics = _summarize_run(run_dir, case_keys)
    expected = len(case_keys)
    attempts = [
        row for row in read_jsonl(run_dir / "api_attempts.jsonl")
        if isinstance(row, dict) and row.get("batch_id") in set(case_keys)
    ]
    received_by_case = _received_attempts_by_case(attempts)
    final_received = [rows[-1] for rows in received_by_case.values() if rows]
    run_config = json.loads((run_dir / "run_config.json").read_text(encoding="utf-8"))
    completion_cap = run_config["generation"]["max_completion_tokens"]
    cap_hits = [
        row for row in final_received
        if row.get("finish_reason") == "length"
        or (
            completion_cap is not None
            and row.get("completion_tokens") is not None
            and row["completion_tokens"] >= completion_cap
        )
    ]
    format_valid_rate = metrics["scored_case_count"] / expected if expected else None
    api_error_rate = metrics["api_error_count"] / expected if expected else None
    cap_hit_rate = len(cap_hits) / expected if expected else None
    p90 = metrics["response_latency_seconds"]["p90"]
    criteria = {
        "format_valid_rate": format_valid_rate is not None and format_valid_rate >= min_format_valid_rate,
        "expanded_block_accuracy": (
            metrics["expanded_block_exact_rate"] is not None
            and metrics["expanded_block_exact_rate"] >= min_expanded_block_rate
        ),
        "p90_latency": p90 is not None and p90 <= max_p90_seconds,
        "api_error_rate": api_error_rate is not None and api_error_rate <= max_api_error_rate,
        "completion_cap_hit_rate": cap_hit_rate is not None and cap_hit_rate <= max_cap_hit_rate,
    }
    return {
        "schema_version": ROLLOUT_SCHEMA_VERSION,
        "case_count": expected,
        "thresholds": {
            "min_format_valid_rate": min_format_valid_rate,
            "min_expanded_block_rate": min_expanded_block_rate,
            "max_p90_seconds": max_p90_seconds,
            "max_api_error_rate": max_api_error_rate,
            "max_completion_cap_hit_rate": max_cap_hit_rate,
        },
        "metrics": {
            **metrics,
            "format_valid_rate": format_valid_rate,
            "api_error_rate": api_error_rate,
            "completion_cap": completion_cap,
            "completion_cap_hit_count": len(cap_hits),
            "completion_cap_hit_rate": cap_hit_rate,
            "completion_cap_hit_case_keys": [row.get("batch_id") for row in cap_hits],
            "max_observed_completion_tokens": max(
                (row.get("completion_tokens", 0) for row in final_received),
                default=None,
            ),
        },
        "criteria": criteria,
        "all_criteria_pass": all(criteria.values()),
    }


def _load_gate_case_keys(case_keys_file=None, model_batches_path=None):
    if (case_keys_file is None) == (model_batches_path is None):
        raise ValueError("provide exactly one of case_keys_file or model_batches_path")
    if case_keys_file is not None:
        case_keys = [
            line.strip()
            for line in Path(case_keys_file).read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
    else:
        case_keys = [
            row.get("batch_id")
            for row in read_jsonl(model_batches_path)
            if isinstance(row, dict) and row.get("batch_id")
        ]
    if not case_keys:
        raise ValueError("gate case selection is empty")
    if len(case_keys) != len(set(case_keys)):
        raise ValueError("gate case selection contains duplicate case keys")
    return case_keys


def write_rollout_gate(
    run_dir,
    case_keys_file,
    output_dir,
    model_batches_path=None,
    report_filename="MIDTERM_REPORT.md",
    report_title="Full rollout midterm gate",
    **thresholds,
):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    case_keys = _load_gate_case_keys(case_keys_file, model_batches_path)
    result = check_rollout_gates(run_dir, case_keys, **thresholds)
    write_json(output_dir / "gate.json", result)
    metrics = result["metrics"]
    criteria_rows = "\n".join(
        f"- {name}: {'PASS' if passed else 'FAIL'}"
        for name, passed in result["criteria"].items()
    )
    report = f"""# {report_title}

Cases: {result['case_count']}

| Metric | Value | Threshold |
|---|---:|---:|
| Format valid | {_format_percent(metrics['format_valid_rate'])} | >= {_format_percent(result['thresholds']['min_format_valid_rate'])} |
| Expanded Block exact | {_format_percent(metrics['expanded_block_exact_rate'])} | >= {_format_percent(result['thresholds']['min_expanded_block_rate'])} |
| Response P90 seconds | {metrics['response_latency_seconds']['p90']} | <= {result['thresholds']['max_p90_seconds']} |
| API error rate | {_format_percent(metrics['api_error_rate'])} | <= {_format_percent(result['thresholds']['max_api_error_rate'])} |
| 8192-token cap hit rate | {_format_percent(metrics['completion_cap_hit_rate'])} | <= {_format_percent(result['thresholds']['max_completion_cap_hit_rate'])} |

## Criteria

{criteria_rows}

Overall: {'PASS' if result['all_criteria_pass'] else 'FAIL'}
"""
    (output_dir / report_filename).write_text(report, encoding="utf-8")
    return result


def main():
    parser = argparse.ArgumentParser(description="Select and compare paired block/state API canaries.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    select_parser = subparsers.add_parser("select")
    select_parser.add_argument("--baseline-dir", required=True)
    select_parser.add_argument("--output-dir", required=True)
    select_parser.add_argument("--sample-size", type=int, default=40)

    compare_parser = subparsers.add_parser("compare")
    compare_parser.add_argument("--baseline-dir", required=True)
    compare_parser.add_argument("--candidate-dir", required=True)
    compare_parser.add_argument("--case-keys-file", required=True)
    compare_parser.add_argument("--output-dir", required=True)
    rollout_select_parser = subparsers.add_parser("select-full")
    rollout_select_parser.add_argument("--model-batches", required=True)
    rollout_select_parser.add_argument("--output-dir", required=True)
    rollout_select_parser.add_argument("--sample-size", type=int, default=300)

    gate_parser = subparsers.add_parser("gate")
    gate_parser.add_argument("--run-dir", required=True)
    gate_source_parser = gate_parser.add_mutually_exclusive_group(required=True)
    gate_source_parser.add_argument("--case-keys-file")
    gate_source_parser.add_argument("--model-batches")
    gate_parser.add_argument("--output-dir", required=True)
    gate_parser.add_argument("--report-filename", default="MIDTERM_REPORT.md")
    gate_parser.add_argument("--report-title", default="Full rollout midterm gate")
    gate_parser.add_argument("--min-format-valid-rate", type=float, default=0.95)
    gate_parser.add_argument("--min-expanded-block-rate", type=float, default=0.725)
    gate_parser.add_argument("--max-p90-seconds", type=float, default=60.0)
    gate_parser.add_argument("--max-api-error-rate", type=float, default=0.02)
    gate_parser.add_argument("--max-cap-hit-rate", type=float, default=0.01)
    args = parser.parse_args()
    if args.command == "select":
        result = write_selection(args.baseline_dir, args.output_dir, args.sample_size)
    elif args.command == "compare":
        result = write_comparison(
            args.baseline_dir,
            args.candidate_dir,
            args.case_keys_file,
            args.output_dir,
        )
    elif args.command == "select-full":
        result = write_full_rollout_selection(
            args.model_batches,
            args.output_dir,
            args.sample_size,
        )
    else:
        result = write_rollout_gate(
            args.run_dir,
            args.case_keys_file,
            args.output_dir,
            model_batches_path=args.model_batches,
            report_filename=args.report_filename,
            report_title=args.report_title,
            min_format_valid_rate=args.min_format_valid_rate,
            min_expanded_block_rate=args.min_expanded_block_rate,
            max_p90_seconds=args.max_p90_seconds,
            max_api_error_rate=args.max_api_error_rate,
            max_cap_hit_rate=args.max_cap_hit_rate,
        )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
