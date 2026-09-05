"""Freeze full cohorts and build a deterministic stratified canary plan."""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

from granularity3_local.decomposed_core import compact_json, read_json, read_jsonl
from granularity3_local.decomposed_prepare import stable_ids_sha256
from granularity3_local.oracle import write_json, write_jsonl


PLAN_SCHEMA_VERSION = "g3-decomposed-plan-v1"
LENGTH_BIN_ORDER = ("2", "3-5", "6-10", "11-25", "26-100", ">100")


def state_length_bin(length):
    if length <= 2:
        return "2"
    if length <= 5:
        return "3-5"
    if length <= 10:
        return "6-10"
    if length <= 25:
        return "11-25"
    if length <= 100:
        return "26-100"
    return ">100"


def value_kinds(value):
    kinds = set()

    def visit(item):
        if isinstance(item, dict):
            if set(item) == {"$u"}:
                kinds.add("undefined")
            elif set(item) == {"$t"}:
                kinds.add("tuple")
                visit(item["$t"])
            elif set(item) == {"$d"}:
                kinds.add("dict")
                visit(item["$d"])
            elif set(item) == {"$s"}:
                kinds.add("set")
                visit(item["$s"])
            elif set(item) == {"$f"}:
                kinds.add("special_float")
            else:
                kinds.add("object")
                for child in item.values():
                    visit(child)
        elif isinstance(item, list):
            kinds.add("list")
            for child in item:
                visit(child)
        elif item is None:
            kinds.add("null")
        elif isinstance(item, bool):
            kinds.add("bool")
        elif isinstance(item, (int, float)):
            kinds.add("number")
        elif isinstance(item, str):
            kinds.add("string")
        else:
            kinds.add(type(item).__name__)

    visit(value)
    return sorted(kinds)


def _quantile(sorted_values, fraction):
    if not sorted_values:
        return None
    return sorted_values[int((len(sorted_values) - 1) * fraction)]


def _distribution(values):
    values = sorted(values)
    return {
        "count": len(values),
        "min": values[0] if values else None,
        "p50": _quantile(values, 0.50),
        "p90": _quantile(values, 0.90),
        "p99": _quantile(values, 0.99),
        "max": values[-1] if values else None,
    }


def _legacy_case_keys(rows):
    result = []
    for row in rows:
        case_key = row.get("batch_id") or row.get("case_key")
        if isinstance(case_key, str) and "/" in case_key:
            result.append(case_key)
    return list(dict.fromkeys(result))


def _write_lines(path, values):
    Path(path).write_text("".join(f"{value}\n" for value in values), encoding="utf-8")


def _candidate_rows(prepared_dir):
    prepared_dir = Path(prepared_dir)
    manifests = {
        row["case_key"]: row
        for row in read_jsonl(prepared_dir / "case_manifest.jsonl")
    }
    requests = read_jsonl(prepared_dir / "oracle_state" / "requests.jsonl")
    oracles = {
        row["request_id"]: row
        for row in read_jsonl(prepared_dir / "oracle_state" / "oracles.jsonl")
    }
    candidates = []
    for request in requests:
        oracle = oracles[request["request_id"]]
        states = oracle["answer"]["states"]
        manifest = manifests[request["case_key"]]
        candidates.append({
            "request_id": request["request_id"],
            "case_key": request["case_key"],
            "task_id": request["task_id"],
            "input_id": request["input_id"],
            "target_variable": request["target_variable"],
            "state_length": len(states),
            "state_length_bin": state_length_bin(len(states)),
            "state_answer_chars": len(compact_json(oracle["answer"])),
            "request_chars": len(compact_json(request["request"])),
            "statement_event_count": manifest["statement_event_count"],
            "control_run_count": manifest["control_run_count"],
            "tracked_variable_count": manifest["tracked_variable_count"],
            "value_kinds": value_kinds(states),
        })
    return candidates


def select_stratified_canary(candidates, case_count):
    if case_count < 1:
        raise ValueError("canary case_count must be positive")
    bins = defaultdict(list)
    for row in candidates:
        bins[row["state_length_bin"]].append(row)
    for rows in bins.values():
        rows.sort(
            key=lambda row: (
                row["state_length"],
                row["state_answer_chars"],
                row["statement_event_count"],
                row["request_chars"],
                row["request_id"],
            ),
            reverse=True,
        )

    selected = []
    selected_cases = set()
    selected_tasks = Counter()
    positions = {label: 0 for label in LENGTH_BIN_ORDER}
    while len(selected) < case_count:
        progress = False
        for label in LENGTH_BIN_ORDER:
            rows = bins.get(label, [])
            if not rows or len(selected) >= case_count:
                continue
            start = positions[label]
            choice = None
            for prefer_new_task in (True, False):
                for index in range(start, len(rows)):
                    row = rows[index]
                    if row["case_key"] in selected_cases:
                        continue
                    if prefer_new_task and selected_tasks[row["task_id"]]:
                        continue
                    choice = (index, row)
                    break
                if choice is not None:
                    break
            if choice is None:
                positions[label] = len(rows)
                continue
            index, row = choice
            rows[start], rows[index] = rows[index], rows[start]
            positions[label] = start + 1
            selected.append(row)
            selected_cases.add(row["case_key"])
            selected_tasks[row["task_id"]] += 1
            progress = True
        if not progress:
            break
    if len(selected) < case_count:
        raise ValueError(
            f"only {len(selected)} unique state cases are available for canary"
        )
    return selected


def build_experiment_plan(
    prepared_dir,
    output_dir,
    legacy_selected_requests=None,
    canary_case_count=40,
):
    prepared_dir = Path(prepared_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    cohort = read_json(prepared_dir / "cohort.json")
    summary = read_json(prepared_dir / "summary.json")
    control_requests = read_jsonl(prepared_dir / "control_flow" / "requests.jsonl")
    state_requests = read_jsonl(prepared_dir / "oracle_state" / "requests.jsonl")
    state_oracles = read_jsonl(prepared_dir / "oracle_state" / "oracles.jsonl")
    excluded = read_jsonl(prepared_dir / "excluded.jsonl")
    full_case_keys = [row["case_key"] for row in control_requests]
    full_case_set = set(full_case_keys)

    legacy_case_keys = []
    if legacy_selected_requests:
        legacy_case_keys = _legacy_case_keys(read_jsonl(legacy_selected_requests))
    legacy_case_set = set(legacy_case_keys)
    shared_case_keys = [key for key in full_case_keys if key in legacy_case_set]
    decomposed_only_case_keys = [key for key in full_case_keys if key not in legacy_case_set]

    candidates = _candidate_rows(prepared_dir)
    canary = select_stratified_canary(candidates, canary_case_count)
    canary_control_ids = [row["case_key"] for row in canary]
    canary_state_ids = [row["request_id"] for row in canary]
    canary_predicted_ids = [
        request_id.replace("/state/", "/predicted_state/", 1)
        for request_id in canary_state_ids
    ]

    state_lengths = [len(row["answer"]["states"]) for row in state_oracles]
    state_answer_chars = [len(compact_json(row["answer"])) for row in state_oracles]
    control_request_chars = [len(compact_json(row["request"])) for row in control_requests]
    state_request_chars = [len(compact_json(row["request"])) for row in state_requests]
    exclusion_reasons = Counter(
        f"{row.get('scope')}:{row.get('reason')}" for row in excluded
    )
    plan_summary = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "prepare_schema_version": summary["schema_version"],
        "protocol_schema_version": summary["protocol_schema_version"],
        "full_case_count": len(full_case_keys),
        "full_case_keys_sha256": stable_ids_sha256(full_case_keys),
        "state_case_count": summary["state_case_count"],
        "state_case_coverage_rate": summary["state_case_coverage_rate"],
        "state_request_count": len(state_requests),
        "state_request_ids_sha256": cohort["state_request_ids_sha256"],
        "legacy_case_count": len(legacy_case_keys),
        "shared_legacy_case_count": len(shared_case_keys),
        "decomposed_only_case_count": len(decomposed_only_case_keys),
        "canary_case_count": len(canary),
        "canary_control_request_ids_sha256": stable_ids_sha256(canary_control_ids),
        "canary_state_request_ids_sha256": stable_ids_sha256(canary_state_ids),
        "control_request_chars": _distribution(control_request_chars),
        "state_request_chars": _distribution(state_request_chars),
        "state_answer_chars": _distribution(state_answer_chars),
        "state_lengths": _distribution(state_lengths),
        "state_length_bin_counts": dict(sorted(Counter(
            state_length_bin(length) for length in state_lengths
        ).items())),
        "canary_state_length_bin_counts": dict(sorted(Counter(
            row["state_length_bin"] for row in canary
        ).items())),
        "canary_value_kind_counts": dict(sorted(Counter(
            kind for row in canary for kind in row["value_kinds"]
        ).items())),
        "exclusion_reason_counts": dict(sorted(exclusion_reasons.items())),
    }
    write_json(output_dir / "summary.json", plan_summary)
    write_jsonl(output_dir / "canary_cases.jsonl", canary)
    _write_lines(output_dir / "full_control_request_ids.txt", full_case_keys)
    _write_lines(
        output_dir / "full_oracle_state_request_ids.txt",
        [row["request_id"] for row in state_requests],
    )
    _write_lines(output_dir / "shared_legacy_case_keys.txt", shared_case_keys)
    _write_lines(output_dir / "decomposed_only_case_keys.txt", decomposed_only_case_keys)
    _write_lines(output_dir / "canary_control_request_ids.txt", canary_control_ids)
    _write_lines(output_dir / "canary_oracle_state_request_ids.txt", canary_state_ids)
    _write_lines(output_dir / "canary_predicted_state_request_ids.txt", canary_predicted_ids)
    return {"summary": plan_summary, "canary": canary}


def main():
    parser = argparse.ArgumentParser(
        description="Freeze decomposed full cohorts and select a stratified canary."
    )
    parser.add_argument("--prepared-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--legacy-selected-requests")
    parser.add_argument("--canary-case-count", type=int, default=40)
    args = parser.parse_args()
    result = build_experiment_plan(
        prepared_dir=args.prepared_dir,
        output_dir=args.output_dir,
        legacy_selected_requests=args.legacy_selected_requests,
        canary_case_count=args.canary_case_count,
    )
    print(json.dumps(result["summary"], ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
