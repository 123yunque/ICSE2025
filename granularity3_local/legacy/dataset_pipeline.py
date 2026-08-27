import argparse
import json
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from granularity3_local.isolated import run_isolated_case
from granularity3_local.oracle import write_json
from granularity3_local.preflight import preflight_task


def task_sort_key(path):
    try:
        return int(path.name.rsplit("_", 1)[1])
    except Exception:
        return path.name


def read_inputs(task_dir, limit):
    rows = [
        line.strip() for line in (Path(task_dir) / "code_inputs.txt").read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    return rows[:limit] if limit is not None else rows


def read_existing_records(path):
    records = []
    if not Path(path).exists():
        return records
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(json.loads(line))
    return records


def append_record(path, record):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with Path(path).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def summarize(preflight_rows, case_records, started_at, config):
    statuses = Counter(row["status"] for row in case_records)
    successful = [row for row in case_records if row["status"] in {"success", "cached"}]
    events = sorted(row.get("event_count", 0) for row in successful)
    sizes = sorted(row.get("size_bytes", 0) for row in successful if "size_bytes" in row)

    def percentile(values, fraction):
        return values[int((len(values) - 1) * fraction)] if values else None

    return {
        "schema_version": "g3-dataset-local-v1",
        "config": config,
        "elapsed_seconds": time.time() - started_at,
        "task_count": len(preflight_rows),
        "supported_task_count": sum(row["supported"] for row in preflight_rows),
        "preflight_statuses": dict(Counter(row["status"] for row in preflight_rows)),
        "planned_case_count": sum(row.get("planned_inputs", 0) for row in preflight_rows if row["supported"]),
        "recorded_case_count": len(case_records),
        "case_statuses": dict(statuses),
        "successful_case_count": len(successful),
        "event_count": sum(row.get("event_count", 0) for row in successful),
        "probe_count": sum(row.get("probe_count", 0) for row in successful),
        "event_distribution": {
            "min": min(events) if events else None,
            "p50": percentile(events, 0.50),
            "p90": percentile(events, 0.90),
            "p99": percentile(events, 0.99),
            "max": max(events) if events else None,
        },
        "size_distribution_bytes": {
            "p50": percentile(sizes, 0.50),
            "p90": percentile(sizes, 0.90),
            "max": max(sizes) if sizes else None,
        },
        "unsupported_tasks": [row for row in preflight_rows if not row["supported"]],
        "failed_cases": [row for row in case_records if row["status"] not in {"success", "cached"}],
    }


def run_dataset_pipeline(
    dataset_root,
    output_root,
    inputs_per_task=3,
    workers=4,
    timeout_seconds=5.0,
    max_events=10000,
    max_output_bytes=20_000_000,
    task_limit=None,
):
    dataset_root = Path(dataset_root)
    output_root = Path(output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    tasks = sorted((path for path in dataset_root.glob("task_*") if path.is_dir()), key=task_sort_key)
    if task_limit is not None:
        tasks = tasks[:task_limit]

    preflight_rows = []
    jobs = []
    for task_dir in tasks:
        preflight = preflight_task(task_dir)
        inputs = read_inputs(task_dir, inputs_per_task)
        preflight["planned_inputs"] = len(inputs)
        preflight_rows.append(preflight)
        if not preflight["supported"]:
            continue
        for index, input_text in enumerate(inputs, start=1):
            input_id = f"input_{index}"
            jobs.append({
                "case_key": f"{task_dir.name}/original/{input_id}",
                "task_dir": task_dir,
                "function_name": preflight["function"],
                "input_text": input_text,
                "input_id": input_id,
            })

    write_json(output_root / "preflight.json", {"tasks": preflight_rows})
    records_path = output_root / "case_records.jsonl"
    existing = read_existing_records(records_path)
    finished_keys = {row["case_key"] for row in existing if row["status"] in {"success", "cached"}}
    pending = [job for job in jobs if job["case_key"] not in finished_keys]
    started_at = time.time()
    config = {
        "dataset_root": str(dataset_root.resolve()),
        "output_root": str(output_root.resolve()),
        "inputs_per_task": inputs_per_task,
        "workers": workers,
        "timeout_seconds": timeout_seconds,
        "max_events": max_events,
        "max_output_bytes": max_output_bytes,
    }

    def execute(job):
        result = run_isolated_case(
            code_path=job["task_dir"] / "code.py",
            function_name=job["function_name"],
            input_text=job["input_text"],
            task_id=job["task_dir"].name,
            variant="original",
            input_id=job["input_id"],
            output_dir=output_root / "cases" / job["task_dir"].name / "original" / job["input_id"],
            timeout_seconds=timeout_seconds,
            max_events=max_events,
            max_output_bytes=max_output_bytes,
        )
        return {
            "case_key": job["case_key"],
            "task_id": job["task_dir"].name,
            "input_id": job["input_id"],
            "input": job["input_text"],
            "function": job["function_name"],
            **result,
        }

    completed_count = 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {executor.submit(execute, job): job for job in pending}
        for future in as_completed(future_map):
            job = future_map[future]
            try:
                record = future.result()
            except Exception as exc:
                record = {
                    "case_key": job["case_key"],
                    "task_id": job["task_dir"].name,
                    "input_id": job["input_id"],
                    "input": job["input_text"],
                    "function": job["function_name"],
                    "status": "orchestrator_error",
                    "error_type": type(exc).__name__,
                    "reason": str(exc),
                }
            append_record(records_path, record)
            existing.append(record)
            completed_count += 1
            if completed_count == 1 or completed_count % 50 == 0 or completed_count == len(pending):
                print(f"[local-dataset] {completed_count}/{len(pending)} {record['case_key']} {record['status']}", flush=True)

    latest_by_key = {}
    for record in existing:
        latest_by_key[record["case_key"]] = record
    summary = summarize(preflight_rows, list(latest_by_key.values()), started_at, config)
    write_json(output_root / "summary.json", summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description="Run isolated pre-LLM local generation over a standardized dataset.")
    parser.add_argument("--dataset-root", required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--inputs-per-task", type=int, default=3)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--timeout-seconds", type=float, default=5.0)
    parser.add_argument("--max-events", type=int, default=10000)
    parser.add_argument("--max-output-bytes", type=int, default=20_000_000)
    parser.add_argument("--task-limit", type=int)
    args = parser.parse_args()
    summary = run_dataset_pipeline(
        dataset_root=args.dataset_root,
        output_root=args.output_root,
        inputs_per_task=args.inputs_per_task,
        workers=args.workers,
        timeout_seconds=args.timeout_seconds,
        max_events=args.max_events,
        max_output_bytes=args.max_output_bytes,
        task_limit=args.task_limit,
    )
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()

