import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

from granularity3_local.oracle import write_json


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def read_jsonl(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def validate_case(case_dir):
    case_dir = Path(case_dir)
    problems = []
    required = [
        "oracle/case.json",
        "oracle/cfg.json",
        "oracle/events.jsonl",
        "oracle/hashes.json",
        "probes/model_case.json",
        "probes/model_inputs.jsonl",
        "probes/answers.jsonl",
        "probes/manifest.json",
    ]
    for relative in required:
        if not (case_dir / relative).exists():
            problems.append(f"missing:{relative}")
    if problems:
        return {"status": "invalid", "problems": problems}

    case = read_json(case_dir / "oracle/case.json")
    events = read_jsonl(case_dir / "oracle/events.jsonl")
    hashes = read_json(case_dir / "oracle/hashes.json")
    model_case = read_json(case_dir / "probes/model_case.json")
    inputs = read_jsonl(case_dir / "probes/model_inputs.jsonl")
    answers = read_jsonl(case_dir / "probes/answers.jsonl")
    manifest = read_json(case_dir / "probes/manifest.json")
    if hashes["case_sha256"] != sha256(case_dir / "oracle/case.json"):
        problems.append("hash:case")
    if hashes["cfg_sha256"] != sha256(case_dir / "oracle/cfg.json"):
        problems.append("hash:cfg")
    if hashes["events_sha256"] != sha256(case_dir / "oracle/events.jsonl"):
        problems.append("hash:events")
    line_trace_path = case_dir / "oracle/line_trace.json"
    if "line_trace_sha256" in hashes:
        if not line_trace_path.exists() or hashes["line_trace_sha256"] != sha256(line_trace_path):
            problems.append("hash:line_trace")
    if not (case["event_count"] == len(events) == len(inputs) == len(answers) == manifest["probe_count"]):
        problems.append("count_mismatch")
    if [row["probe_id"] for row in inputs] != [row["probe_id"] for row in answers]:
        problems.append("probe_id_mismatch")
    forbidden = {"next", "delta", "return", "state_after", "return_value", "next_block"}
    if any(not forbidden.isdisjoint(row) for row in inputs):
        problems.append("answer_leak:model_inputs")
    if not forbidden.isdisjoint(model_case):
        problems.append("answer_leak:model_case")
    if not manifest.get("answer_isolation"):
        problems.append("manifest_answer_isolation_false")
    return {
        "status": "valid" if not problems else "invalid",
        "case_id": case.get("case_id"),
        "event_count": len(events),
        "size_bytes": sum(item.stat().st_size for item in case_dir.rglob("*") if item.is_file()),
        "problems": problems,
    }


def validate_dataset(output_root):
    output_root = Path(output_root)
    records = read_jsonl(output_root / "case_records.jsonl")
    latest = {}
    for record in records:
        latest[record["case_key"]] = record
    valid_records = [record for record in latest.values() if record["status"] in {"success", "cached"}]
    validations = []
    for record in valid_records:
        case_dir = output_root / "cases" / record["task_id"] / "original" / record["input_id"]
        validations.append({"case_key": record["case_key"], **validate_case(case_dir)})
    statuses = Counter(row["status"] for row in validations)
    sizes = sorted(row["size_bytes"] for row in validations if row["status"] == "valid")
    report = {
        "schema_version": "g3-dataset-validation-v1",
        "expected_case_count": len(valid_records),
        "validated_case_count": len(validations),
        "statuses": dict(statuses),
        "valid_event_count": sum(row.get("event_count", 0) for row in validations if row["status"] == "valid"),
        "total_size_bytes": sum(sizes),
        "max_case_size_bytes": max(sizes) if sizes else None,
        "invalid_cases": [row for row in validations if row["status"] != "valid"],
    }
    write_json(output_root / "validation.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description="Validate hashes, counts, alignment, and answer isolation.")
    parser.add_argument("--output-root", required=True)
    args = parser.parse_args()
    report = validate_dataset(args.output_root)
    print(json.dumps(report, ensure_ascii=False))
    if report["invalid_cases"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
