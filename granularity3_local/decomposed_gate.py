"""Quality gate for staged decomposed API rollouts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from granularity3_local.decomposed_core import read_json, read_jsonl
from granularity3_local.oracle import write_json


GATE_SCHEMA_VERSION = "g3-decomposed-gate-v1"


def _token_summary(attempts):
    received = [row for row in attempts if row.get("status") == "received"]
    return {
        "received_attempt_count": len(received),
        "prompt_tokens": sum(row.get("prompt_tokens", 0) for row in received),
        "completion_tokens": sum(
            row.get("completion_tokens", 0) for row in received
        ),
        "reasoning_tokens": sum(row.get("reasoning_tokens", 0) for row in received),
        "non_stop_finish_count": sum(
            row.get("finish_reason") not in (None, "stop") for row in received
        ),
        "max_prompt_tokens": max(
            (row.get("prompt_tokens", 0) for row in received), default=0
        ),
        "max_completion_tokens": max(
            (row.get("completion_tokens", 0) for row in received), default=0
        ),
    }


def build_quality_gate(
    run_dirs,
    output_path=None,
    min_format_valid_rate=0.95,
    require_complete=True,
):
    checks = []
    runs = {}
    for label, run_dir in run_dirs.items():
        run_dir = Path(run_dir)
        summary = read_json(run_dir / "summary.json")
        attempts = read_jsonl(run_dir / "api_attempts.jsonl")
        evaluation = summary["evaluation"]
        token_summary = _token_summary(attempts)
        runs[label] = {
            "selected_request_count": summary["selected_request_count"],
            "response_count": summary["response_count"],
            "api_error_count": summary["api_error_count"],
            "invalid_attempt_count": summary["invalid_attempt_count"],
            "format_valid_rate": evaluation["format_valid_rate"],
            "response_complete": evaluation["response_complete"],
            **token_summary,
        }
        checks.extend([
            {
                "name": f"{label}.format_valid_rate",
                "passed": evaluation["format_valid_rate"] >= min_format_valid_rate,
                "actual": evaluation["format_valid_rate"],
                "required": min_format_valid_rate,
            },
            {
                "name": f"{label}.response_complete",
                "passed": (
                    evaluation["response_complete"] if require_complete else True
                ),
                "actual": evaluation["response_complete"],
                "required": require_complete,
            },
            {
                "name": f"{label}.finish_reason",
                "passed": token_summary["non_stop_finish_count"] == 0,
                "actual": token_summary["non_stop_finish_count"],
                "required": 0,
            },
        ])
    result = {
        "schema_version": GATE_SCHEMA_VERSION,
        "passed": all(row["passed"] for row in checks),
        "min_format_valid_rate": min_format_valid_rate,
        "require_complete": require_complete,
        "checks": checks,
        "runs": runs,
    }
    if output_path is not None:
        write_json(output_path, result)
    return result


def _parse_run(value):
    if "=" not in value:
        raise argparse.ArgumentTypeError("run must be LABEL=PATH")
    label, path = value.split("=", 1)
    if not label or not path:
        raise argparse.ArgumentTypeError("run must be LABEL=PATH")
    return label, path


def main():
    parser = argparse.ArgumentParser(description="Gate decomposed staged API runs.")
    parser.add_argument(
        "--run",
        action="append",
        type=_parse_run,
        required=True,
        help="LABEL=PATH; repeat for control/oracle/predicted runs.",
    )
    parser.add_argument("--output", required=True)
    parser.add_argument("--min-format-valid-rate", type=float, default=0.95)
    parser.add_argument("--allow-incomplete", action="store_true")
    args = parser.parse_args()
    result = build_quality_gate(
        run_dirs=dict(args.run),
        output_path=args.output,
        min_format_valid_rate=args.min_format_valid_rate,
        require_complete=not args.allow_incomplete,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
