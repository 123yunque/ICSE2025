import argparse
import json
from pathlib import Path

from granularity3_local.oracle import build_oracle_case
from granularity3_local.legacy.probes import build_probe_dataset


def directory_size(path):
    return sum(item.stat().st_size for item in Path(path).rglob("*") if item.is_file())


def main():
    parser = argparse.ArgumentParser(description="Internal isolated local-oracle worker.")
    parser.add_argument("--request", required=True)
    parser.add_argument("--response", required=True)
    args = parser.parse_args()
    request = json.loads(Path(args.request).read_text(encoding="utf-8"))
    response_path = Path(args.response)
    try:
        code_path = Path(request["code_path"])
        source = code_path.read_text(encoding="utf-8")
        case_dir = Path(request["case_dir"])
        oracle_dir = case_dir / "oracle"
        probe_dir = case_dir / "probes"
        oracle = build_oracle_case(
            source=source,
            function_name=request["function_name"],
            input_text=request["input_text"],
            task_id=request["task_id"],
            variant=request["variant"],
            input_id=request["input_id"],
            output_dir=oracle_dir,
            source_path=code_path,
            max_events=request["max_events"],
            max_trace_bytes=request.get("max_trace_bytes"),
        )
        probes = build_probe_dataset(oracle_dir, probe_dir)
        size_bytes = directory_size(case_dir)
        if size_bytes > request["max_output_bytes"]:
            response = {
                "status": "output_too_large",
                "size_bytes": size_bytes,
                "max_output_bytes": request["max_output_bytes"],
            }
        else:
            response = {
                "status": "success",
                "result": oracle["case"]["result"],
                "event_count": oracle["case"]["event_count"],
                "probe_count": probes["manifest"]["probe_count"],
                "size_bytes": size_bytes,
                "hashes": oracle["hashes"],
            }
    except Exception as exc:
        response = {
            "status": "failed",
            "error_type": type(exc).__name__,
            "reason": str(exc),
        }
    response_path.write_text(json.dumps(response, ensure_ascii=False, sort_keys=True), encoding="utf-8")


if __name__ == "__main__":
    main()
