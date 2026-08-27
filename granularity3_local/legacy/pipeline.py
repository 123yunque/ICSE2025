import argparse
import json
from pathlib import Path

from granularity3_local.oracle import build_oracle_case, choose_function, write_json
from granularity3_local.legacy.probes import build_probe_dataset


def run_local_pipeline(
    task_dir,
    output_root,
    variant="original",
    function_name=None,
    input_file="code_inputs.txt",
    limit=None,
    max_events=10000,
):
    task_dir = Path(task_dir)
    output_root = Path(output_root)
    source_path = task_dir / "code.py"
    source = source_path.read_text(encoding="utf-8")
    function_name = choose_function(source, function_name)
    input_lines = [
        line.strip() for line in (task_dir / input_file).read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    if limit is not None:
        input_lines = input_lines[:limit]

    records = []
    for index, input_text in enumerate(input_lines, start=1):
        input_id = f"input_{index}"
        case_root = output_root / "cases" / task_dir.name / variant / input_id
        oracle_dir = case_root / "oracle"
        probe_dir = case_root / "probes"
        try:
            oracle = build_oracle_case(
                source=source,
                function_name=function_name,
                input_text=input_text,
                task_id=task_dir.name,
                variant=variant,
                input_id=input_id,
                output_dir=oracle_dir,
                source_path=source_path,
                max_events=max_events,
            )
            probes = build_probe_dataset(oracle_dir, probe_dir)
            records.append({
                "input_id": input_id,
                "input": input_text,
                "status": "success",
                "result": oracle["case"]["result"],
                "event_count": oracle["case"]["event_count"],
                "probe_count": probes["manifest"]["probe_count"],
                "oracle_dir": str(oracle_dir),
                "probe_dir": str(probe_dir),
            })
        except Exception as exc:
            records.append({
                "input_id": input_id,
                "input": input_text,
                "status": "failed",
                "error_type": type(exc).__name__,
                "reason": str(exc),
            })

    successes = [record for record in records if record["status"] == "success"]
    summary = {
        "schema_version": "g3-local-pipeline-v1",
        "task_id": task_dir.name,
        "variant": variant,
        "function": function_name,
        "input_count": len(records),
        "success_count": len(successes),
        "failure_count": len(records) - len(successes),
        "event_count": sum(record["event_count"] for record in successes),
        "probe_count": sum(record["probe_count"] for record in successes),
        "records": records,
    }
    output_root.mkdir(parents=True, exist_ok=True)
    write_json(output_root / f"{task_dir.name}.{variant}.summary.json", summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description="Run every pre-LLM local stage for one standardized task directory.")
    parser.add_argument("--task-dir", required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--variant", default="original")
    parser.add_argument("--function")
    parser.add_argument("--input-file", default="code_inputs.txt")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--max-events", type=int, default=10000)
    args = parser.parse_args()
    summary = run_local_pipeline(
        task_dir=args.task_dir,
        output_root=args.output_root,
        variant=args.variant,
        function_name=args.function,
        input_file=args.input_file,
        limit=args.limit,
        max_events=args.max_events,
    )
    print(json.dumps(summary, ensure_ascii=False))
    if summary["failure_count"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

