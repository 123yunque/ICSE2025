import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from granularity3_local.block_state_local import prepare_local_case
from granularity3_local.oracle import write_json
from granularity3_local.preflight import preflight_task


SCHEMA_VERSION = "g3-block-state-dataset-v1"
PACKAGE_ROOT = Path(__file__).resolve().parent.parent


def task_sort_key(path):
    try:
        return int(path.name.rsplit("_", 1)[1])
    except (IndexError, ValueError):
        return path.name


def read_inputs(task_dir, limit):
    path = Path(task_dir) / "code_inputs.txt"
    if not path.exists():
        return []
    rows = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return rows[:limit] if limit is not None else rows


def read_jsonl(path):
    path = Path(path)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def append_jsonl(path, row):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def directory_size(path):
    return sum(item.stat().st_size for item in Path(path).rglob("*") if item.is_file())


def cached_case(case_dir):
    case_dir = Path(case_dir)
    required = [
        case_dir / "manifest.json",
        case_dir / "model_input.json",
        case_dir / "local_answer.json",
        case_dir / "oracle" / "events.jsonl",
        case_dir / "oracle" / "case.json",
    ]
    if not all(path.exists() for path in required):
        return None
    manifest = json.loads((case_dir / "manifest.json").read_text(encoding="utf-8"))
    return {
        "status": "cached",
        "event_count": manifest["event_count"],
        "change_count": manifest["change_count"],
        "model_input_chars": manifest["model_input_chars"],
        "local_answer_chars": manifest["local_answer_chars"],
        "size_bytes": directory_size(case_dir),
    }


def worker_main(request_path, response_path):
    request = json.loads(Path(request_path).read_text(encoding="utf-8"))
    response_path = Path(response_path)
    try:
        source_path = Path(request["source_path"])
        source = source_path.read_text(encoding="utf-8")
        result = prepare_local_case(
            source=source,
            function_name=request["function_name"],
            input_text=request["input_text"],
            task_id=request["task_id"],
            input_id=request["input_id"],
            output_dir=request["case_dir"],
            source_path=source_path,
            max_events=request["max_events"],
            max_trace_bytes=request["max_trace_bytes"],
        )
        size_bytes = directory_size(request["case_dir"])
        if size_bytes > request["max_output_bytes"]:
            response = {
                "status": "output_too_large",
                "size_bytes": size_bytes,
                "max_output_bytes": request["max_output_bytes"],
            }
        else:
            manifest = result["manifest"]
            response = {
                "status": "success",
                "event_count": manifest["event_count"],
                "change_count": manifest["change_count"],
                "model_input_chars": manifest["model_input_chars"],
                "local_answer_chars": manifest["local_answer_chars"],
                "size_bytes": size_bytes,
            }
    except Exception as exc:
        response = {
            "status": "failed",
            "error_type": type(exc).__name__,
            "reason": str(exc),
        }
    response_path.write_text(json.dumps(response, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def run_isolated(job, output_root, timeout_seconds, max_events, max_trace_bytes, max_output_bytes):
    output_root = Path(output_root)
    case_dir = output_root / "cases" / job["task_id"] / job["input_id"]
    cached = cached_case(case_dir)
    if cached is not None:
        return {**job, **cached, "case_dir": str(case_dir.resolve())}
    if case_dir.exists():
        return {
            **job,
            "status": "incomplete_output_exists",
            "case_dir": str(case_dir.resolve()),
        }

    temp_root = output_root / ".tmp"
    temp_root.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="g3-bs-", dir=str(temp_root)) as temp_name:
        temp_dir = Path(temp_name)
        request_path = temp_dir / "request.json"
        response_path = temp_dir / "response.json"
        temp_case = temp_dir / "case"
        request = {
            "source_path": job["source_path"],
            "function_name": job["function"],
            "input_text": job["input"],
            "task_id": job["task_id"],
            "input_id": job["input_id"],
            "case_dir": str(temp_case),
            "max_events": max_events,
            "max_trace_bytes": max_trace_bytes,
            "max_output_bytes": max_output_bytes,
        }
        request_path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
        command = [
            sys.executable,
            "-B",
            "-m",
            "granularity3_local.block_state_dataset",
            "--worker-request",
            str(request_path),
            "--worker-response",
            str(response_path),
        ]
        try:
            completed = subprocess.run(
                command,
                cwd=str(PACKAGE_ROOT),
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return {
                **job,
                "status": "timeout",
                "timeout_seconds": timeout_seconds,
                "elapsed_seconds": time.perf_counter() - started,
            }
        if not response_path.exists():
            return {
                **job,
                "status": "worker_crash",
                "returncode": completed.returncode,
                "stdout": completed.stdout[-1000:],
                "stderr": completed.stderr[-1000:],
                "elapsed_seconds": time.perf_counter() - started,
            }
        response = json.loads(response_path.read_text(encoding="utf-8"))
        response["elapsed_seconds"] = time.perf_counter() - started
        if response["status"] == "success":
            case_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(temp_case), str(case_dir))
            response["case_dir"] = str(case_dir.resolve())
        return {**job, **response}


def percentile(values, fraction):
    if not values:
        return None
    values = sorted(values)
    return values[int((len(values) - 1) * fraction)]


def summarize(preflight_rows, records, config, elapsed_seconds):
    latest = {}
    for row in records:
        latest[row["case_key"]] = row
    rows = list(latest.values())
    successful = [row for row in rows if row["status"] in {"success", "cached"}]
    events = [row["event_count"] for row in successful]
    changes = [row["change_count"] for row in successful]
    return {
        "schema_version": SCHEMA_VERSION,
        "config": config,
        "elapsed_seconds": elapsed_seconds,
        "task_count": len(preflight_rows),
        "supported_task_count": sum(row["supported"] for row in preflight_rows),
        "unsupported_task_count": sum(not row["supported"] for row in preflight_rows),
        "preflight_statuses": dict(Counter(row["status"] for row in preflight_rows)),
        "planned_case_count": config["planned_case_count"],
        "recorded_case_count": len(rows),
        "successful_case_count": len(successful),
        "case_statuses": dict(Counter(row["status"] for row in rows)),
        "event_count": sum(events),
        "change_count": sum(changes),
        "event_distribution": {
            "min": min(events) if events else None,
            "p50": percentile(events, 0.50),
            "p90": percentile(events, 0.90),
            "p99": percentile(events, 0.99),
            "max": max(events) if events else None,
        },
        "change_distribution": {
            "min": min(changes) if changes else None,
            "p50": percentile(changes, 0.50),
            "p90": percentile(changes, 0.90),
            "p99": percentile(changes, 0.99),
            "max": max(changes) if changes else None,
        },
        "model_input_chars": sum(row.get("model_input_chars", 0) for row in successful),
        "local_answer_chars": sum(row.get("local_answer_chars", 0) for row in successful),
        "size_bytes": sum(row.get("size_bytes", 0) for row in successful),
        "unsupported_tasks": [row for row in preflight_rows if not row["supported"]],
        "failed_cases": [row for row in rows if row["status"] not in {"success", "cached"}],
    }


def run_dataset(
    dataset_root,
    output_root,
    inputs_per_task=10,
    workers=8,
    timeout_seconds=10.0,
    max_events=10000,
    max_trace_bytes=5_000_000,
    max_output_bytes=20_000_000,
    task_limit=None,
    resume=False,
):
    dataset_root = Path(dataset_root)
    output_root = Path(output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    tasks = sorted(dataset_root.glob("task_*"), key=task_sort_key)
    tasks = [path for path in tasks if path.is_dir()]
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
            jobs.append({
                "case_key": f"{task_dir.name}/input_{index}",
                "task_id": task_dir.name,
                "input_id": f"input_{index}",
                "input": input_text,
                "function": preflight["function"],
                "source_path": str((task_dir / "code.py").resolve()),
            })

    write_json(output_root / "preflight.json", preflight_rows)
    records_path = output_root / "case_records.jsonl"
    records = read_jsonl(records_path)
    finished = {
        row["case_key"]
        for row in records
        if row.get("status") in {"success", "cached"}
    }
    pending = [job for job in jobs if not (resume and job["case_key"] in finished)]
    config = {
        "dataset_root": str(dataset_root.resolve()),
        "output_root": str(output_root.resolve()),
        "inputs_per_task": inputs_per_task,
        "workers": workers,
        "timeout_seconds": timeout_seconds,
        "max_events": max_events,
        "max_trace_bytes": max_trace_bytes,
        "max_output_bytes": max_output_bytes,
        "task_limit": task_limit,
        "planned_case_count": len(jobs),
        "pending_case_count": len(pending),
        "resume": resume,
    }
    started = time.time()
    completed_count = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                run_isolated,
                job,
                output_root,
                timeout_seconds,
                max_events,
                max_trace_bytes,
                max_output_bytes,
            ): job
            for job in pending
        }
        for future in as_completed(futures):
            job = futures[future]
            try:
                record = future.result()
            except Exception as exc:
                record = {
                    **job,
                    "status": "runner_failed",
                    "error_type": type(exc).__name__,
                    "reason": str(exc),
                }
            append_jsonl(records_path, record)
            records.append(record)
            completed_count += 1
            if completed_count % 25 == 0 or completed_count == len(pending):
                elapsed = time.time() - started
                snapshot = summarize(preflight_rows, records, config, elapsed)
                write_json(output_root / "summary.json", snapshot)
                print(
                    f"[block-state] {completed_count}/{len(pending)} "
                    f"success={snapshot['successful_case_count']} "
                    f"failed={len(snapshot['failed_cases'])}",
                    flush=True,
                )
    summary = summarize(preflight_rows, records, config, time.time() - started)
    write_json(output_root / "summary.json", summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description="Run the block/state local oracle across a dataset.")
    parser.add_argument("--dataset-root")
    parser.add_argument("--output-root")
    parser.add_argument("--inputs-per-task", type=int, default=10)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--timeout-seconds", type=float, default=10.0)
    parser.add_argument("--max-events", type=int, default=10000)
    parser.add_argument("--max-trace-bytes", type=int, default=5_000_000)
    parser.add_argument("--max-output-bytes", type=int, default=20_000_000)
    parser.add_argument("--task-limit", type=int)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--worker-request")
    parser.add_argument("--worker-response")
    args = parser.parse_args()
    if args.worker_request or args.worker_response:
        if not args.worker_request or not args.worker_response:
            parser.error("--worker-request and --worker-response must be used together")
        worker_main(args.worker_request, args.worker_response)
        return
    if not args.dataset_root or not args.output_root:
        parser.error("--dataset-root and --output-root are required")
    summary = run_dataset(
        dataset_root=args.dataset_root,
        output_root=args.output_root,
        inputs_per_task=args.inputs_per_task,
        workers=args.workers,
        timeout_seconds=args.timeout_seconds,
        max_events=args.max_events,
        max_trace_bytes=args.max_trace_bytes,
        max_output_bytes=args.max_output_bytes,
        task_limit=args.task_limit,
        resume=args.resume,
    )
    print(json.dumps({key: value for key, value in summary.items() if key not in {"failed_cases", "unsupported_tasks"}}, ensure_ascii=False))


if __name__ == "__main__":
    main()
