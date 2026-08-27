import argparse
import json
import os
import shutil
import time
from pathlib import Path

from granularity3_local.legacy.api_smoke import exception_chain
from granularity3_local.legacy.joint_pilot import (
    DEFAULT_TASKS,
    build_joint_case,
    call_model,
    read_json,
    score_prediction,
)
from granularity3_local.oracle import build_oracle_case, write_json
from granularity3_local.preflight import resolve_task_function
from granularity3_local.legacy.probes import build_probe_dataset


def read_inputs(task_dir, count, supplemental=None):
    rows = [
        line.strip()
        for line in (Path(task_dir) / "code_inputs.txt").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    official_count = min(len(rows), count)
    result = [{"text": text, "source": "official"} for text in rows[:count]]
    for text in (supplemental or []):
        if len(result) >= count:
            break
        if text not in {item["text"] for item in result}:
            result.append({"text": text, "source": "supplemental"})
    return result, official_count


def prepare_case(task_dir, task_id, input_index, input_text, case_dir, max_probes):
    source_path = Path(task_dir) / "code.py"
    source = source_path.read_text(encoding="utf-8")
    function = resolve_task_function(task_dir)
    case_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, case_dir / "code.py")
    oracle = build_oracle_case(
        source,
        function,
        input_text,
        task_id,
        "original",
        f"input_{input_index}",
        case_dir / "oracle",
        source_path=case_dir / "code.py",
    )
    probes = build_probe_dataset(case_dir / "oracle", case_dir / "probes")
    payload, expected, probe_map = build_joint_case(
        source,
        probes["case"],
        probes["model_inputs"],
        probes["answers"],
        oracle["line_trace"],
        oracle["case"]["result"],
        max_probes=max_probes,
    )
    write_json(case_dir / "model_request.json", payload)
    write_json(case_dir / "local_answer.json", expected)
    write_json(case_dir / "probe_map.json", probe_map)
    return {
        "task_id": task_id,
        "input_id": f"input_{input_index}",
        "function": function,
        "input": input_text,
        "original_probe_count": probes["manifest"]["probe_count"],
        "selected_probe_count": len(payload["probes"]),
        "line_trace_length": len(expected["line_trace"]),
        "request_chars": len(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)),
        "status": "prepared",
    }


def call_with_retry(client, model, payload, timeout, max_attempts):
    errors = []
    for attempt in range(1, max_attempts + 1):
        try:
            prediction, raw, api = call_model(client, model, payload, timeout)
            api["attempts"] = attempt
            return prediction, raw, api, errors
        except Exception as exc:
            errors.append({"attempt": attempt, "error_type": type(exc).__name__, "reason": str(exc)})
            if attempt < max_attempts:
                time.sleep(min(2 ** (attempt - 1), 4))
    raise RuntimeError(json.dumps(errors, ensure_ascii=False))


def aggregate(records):
    successful = [row for row in records if row.get("status") == "success"]
    prepared = [row for row in records if row.get("status") in {"prepared", "success"}]
    probes = sum(row.get("score", {}).get("probe_count", 0) for row in successful)
    summary = {
        "case_count": len(records),
        "prepared_case_count": len(prepared),
        "successful_case_count": len(successful),
        "failed_case_count": sum(row.get("status") == "failed" for row in records),
        "line_trace_exact_count": sum(row["score"]["line_trace_exact"] for row in successful),
        "return_correct_count": sum(row["score"]["return_correct"] for row in successful),
        "joint_exact_count": sum(row["score"]["joint_exact"] for row in successful),
        "joint_semantic_exact_count": sum(row["score"]["joint_semantic_exact"] for row in successful),
        "probe_count": probes,
        "next_correct_count": sum(row["score"]["next_correct_count"] for row in successful),
        "delta_correct_count": sum(row["score"]["delta_correct_count"] for row in successful),
        "delta_semantic_correct_count": sum(row["score"]["delta_semantic_correct_count"] for row in successful),
        "prompt_tokens": sum(row.get("api", {}).get("prompt_tokens") or 0 for row in successful),
        "completion_tokens": sum(row.get("api", {}).get("completion_tokens") or 0 for row in successful),
        "total_tokens": sum(row.get("api", {}).get("total_tokens") or 0 for row in successful),
        "api_elapsed_seconds": sum(row.get("api", {}).get("elapsed_seconds") or 0 for row in successful),
        "records": records,
    }
    count = len(successful)
    summary.update({
        "line_trace_accuracy": summary["line_trace_exact_count"] / count if count else None,
        "return_accuracy": summary["return_correct_count"] / count if count else None,
        "joint_exact_accuracy": summary["joint_exact_count"] / count if count else None,
        "joint_semantic_exact_accuracy": summary["joint_semantic_exact_count"] / count if count else None,
        "next_accuracy": summary["next_correct_count"] / probes if probes else None,
        "delta_accuracy": summary["delta_correct_count"] / probes if probes else None,
        "delta_semantic_accuracy": summary["delta_semantic_correct_count"] / probes if probes else None,
    })
    return summary


def main():
    parser = argparse.ArgumentParser(description="Run a resumable joint granularity-3 benchmark.")
    parser.add_argument("--dataset-root", required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--tasks", nargs="*", default=DEFAULT_TASKS)
    parser.add_argument("--inputs-per-task", type=int, default=10)
    parser.add_argument("--max-probes", type=int, default=8)
    parser.add_argument("--max-line-events", type=int, default=500)
    parser.add_argument("--model", default=os.getenv("YUNWU_MODEL", ""))
    parser.add_argument("--base-url", default=os.getenv("YUNWU_API_BASE_URL", ""))
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--supplemental-inputs", help="JSON mapping task_id to extra input strings.")
    args = parser.parse_args()

    output_root = Path(args.output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    dataset_root = Path(args.dataset_root)
    supplemental_inputs = read_json(args.supplemental_inputs) if args.supplemental_inputs else {}
    client = None
    if not args.prepare_only:
        api_key = os.getenv("YUNWU_API_KEY", "").strip()
        if not api_key or not args.model or not args.base_url:
            raise SystemExit("YUNWU_API_KEY, YUNWU_MODEL, and YUNWU_API_BASE_URL are required")
        from openai import OpenAI
        client = OpenAI(api_key=api_key, base_url=args.base_url, max_retries=0)

    records = []
    try:
        for task_id in args.tasks:
            task_dir = dataset_root / task_id
            inputs, official_count = read_inputs(
                task_dir,
                args.inputs_per_task,
                supplemental=supplemental_inputs.get(task_id),
            )
            for input_index, input_item in enumerate(inputs, start=1):
                input_text = input_item["text"]
                input_source = input_item["source"]
                case_dir = output_root / task_id / f"input_{input_index}"
                record_path = case_dir / "record.json"
                if args.resume and record_path.exists():
                    previous = read_json(record_path)
                    if args.prepare_only or previous.get("status") == "success":
                        records.append(previous)
                        continue
                try:
                    request_path = case_dir / "model_request.json"
                    answer_path = case_dir / "local_answer.json"
                    if request_path.exists() and answer_path.exists():
                        payload = read_json(request_path)
                        expected = read_json(answer_path)
                        record = read_json(record_path) if record_path.exists() else {
                            "task_id": task_id,
                            "input_id": f"input_{input_index}",
                            "function": payload["function"],
                            "input": input_text,
                            "input_source": input_source,
                            "selected_probe_count": len(payload["probes"]),
                            "line_trace_length": len(expected["line_trace"]),
                            "request_chars": len(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)),
                            "status": "prepared",
                        }
                    else:
                        record = prepare_case(task_dir, task_id, input_index, input_text, case_dir, args.max_probes)
                        record["input_source"] = input_source
                        payload = read_json(request_path)
                        expected = read_json(answer_path)
                    if record["line_trace_length"] > args.max_line_events:
                        record["status"] = "skipped_long_trace"
                        record["max_line_events"] = args.max_line_events
                    elif not args.prepare_only:
                        prediction, raw, api, prior_errors = call_with_retry(
                            client, args.model, payload, args.timeout, args.max_attempts
                        )
                        write_json(case_dir / "model_prediction.json", prediction)
                        (case_dir / "raw_response.txt").write_text(raw, encoding="utf-8")
                        score = score_prediction(prediction, expected)
                        write_json(case_dir / "score.json", score)
                        write_json(case_dir / "api.json", api)
                        record.update({"status": "success", "score": score, "api": api, "prior_errors": prior_errors})
                except Exception as exc:
                    record = {
                        "task_id": task_id,
                        "input_id": f"input_{input_index}",
                        "input": input_text,
                        "input_source": input_source,
                        "status": "failed",
                        "error_type": type(exc).__name__,
                        "reason": str(exc),
                        "exception_chain": exception_chain(exc),
                    }
                case_dir.mkdir(parents=True, exist_ok=True)
                write_json(record_path, record)
                records.append(record)
                print(json.dumps({key: value for key, value in record.items() if key not in {"score"}}, ensure_ascii=False), flush=True)
                write_json(output_root / "summary.json", aggregate(records))
    finally:
        if client is not None:
            client.close()
    summary = aggregate(records)
    summary.update({
        "model": args.model,
        "tasks": args.tasks,
        "inputs_per_task": args.inputs_per_task,
        "max_probes_per_case": args.max_probes,
        "max_line_events": args.max_line_events,
        "supplemental_inputs_file": args.supplemental_inputs,
    })
    write_json(output_root / "summary.json", summary)
    print(json.dumps({key: value for key, value in summary.items() if key != "records"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
