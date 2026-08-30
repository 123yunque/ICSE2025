"""Validation and scoring for decomposed granularity-3 model responses."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from granularity3_local.decomposed_core import (
    CONTROL_FLOW_KIND,
    STATE_KINDS,
    ResponseValidationError,
    read_jsonl,
    response_payload_from_record,
    score_control_flow,
    score_state,
    validate_response,
)
from granularity3_local.oracle import write_json, write_jsonl


EVALUATION_SCHEMA_VERSION = "g3-decomposed-evaluation-v1"
COMBINED_SCHEMA_VERSION = "g3-decomposed-combined-v1"


def _unique_by_request_id(rows, label):
    result = {}
    for row in rows:
        request_id = row.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            raise ValueError(f"{label} row needs a non-empty request_id")
        if request_id in result:
            raise ValueError(f"duplicate {label} request_id: {request_id}")
        result[request_id] = row
    return result


def evaluate_response_records(requests, oracles, responses, output_dir=None):
    request_by_id = _unique_by_request_id(requests, "request")
    oracle_by_id = _unique_by_request_id(oracles, "oracle")
    response_by_id = _unique_by_request_id(responses, "response")
    if set(request_by_id) != set(oracle_by_id):
        missing = sorted(set(request_by_id) - set(oracle_by_id))
        extra = sorted(set(oracle_by_id) - set(request_by_id))
        raise ValueError(f"request/oracle ids differ: missing={missing}, extra={extra}")

    kinds = {row["kind"] for row in requests}
    if len(kinds) != 1:
        raise ValueError(f"evaluation requires exactly one task kind, got {sorted(kinds)}")
    kind = next(iter(kinds))
    if any(row.get("kind") != kind for row in oracles):
        raise ValueError("request and oracle kinds differ")

    predictions = []
    scores = []
    errors = []
    received_count = 0
    for request_record in requests:
        request_id = request_record["request_id"]
        oracle_record = oracle_by_id[request_id]
        response_record = response_by_id.get(request_id)
        if response_record is None:
            errors.append({
                "request_id": request_id,
                "case_key": request_record["case_key"],
                "kind": kind,
                "status": "missing_response",
            })
            continue
        received_count += 1
        try:
            payload = response_payload_from_record(response_record)
            predicted = validate_response(request_record, payload)
        except (ResponseValidationError, TypeError, ValueError) as error:
            errors.append({
                "request_id": request_id,
                "case_key": request_record["case_key"],
                "kind": kind,
                "status": "invalid_response",
                "reason": str(error),
            })
            continue

        predictions.append({
            "request_id": request_id,
            "case_key": request_record["case_key"],
            "task_id": request_record["task_id"],
            "input_id": request_record["input_id"],
            "kind": kind,
            "target_variable": request_record.get("target_variable"),
            "prediction": predicted,
        })
        if kind == CONTROL_FLOW_KIND:
            metric = score_control_flow(predicted, oracle_record["answer"])
        elif kind in STATE_KINDS:
            metric = score_state(predicted, oracle_record["answer"])
        else:
            raise ValueError(f"unknown task kind: {kind}")
        scores.append({
            "request_id": request_id,
            "case_key": request_record["case_key"],
            "task_id": request_record["task_id"],
            "input_id": request_record["input_id"],
            "kind": kind,
            "target_variable": request_record.get("target_variable"),
            "parent_state_request_id": request_record.get("parent_state_request_id"),
            **metric,
        })

    expected = len(requests)
    valid = len(scores)
    summary = {
        "schema_version": EVALUATION_SCHEMA_VERSION,
        "kind": kind,
        "expected_request_count": expected,
        "received_response_count": received_count,
        "valid_response_count": valid,
        "invalid_response_count": sum(
            row["status"] == "invalid_response" for row in errors
        ),
        "missing_response_count": sum(
            row["status"] == "missing_response" for row in errors
        ),
        "response_complete": received_count == expected,
        "fully_valid": valid == expected,
        "format_valid_rate": valid / expected if expected else None,
    }
    if kind == CONTROL_FLOW_KIND:
        canonical_count = sum(row["canonical_trace_exact"] for row in scores)
        expanded_count = sum(row["expanded_trace_exact"] for row in scores)
        canonical_format_count = sum(
            row["canonical_format_valid"] for row in scores
        )
        summary.update({
            "canonical_trace_exact_count": canonical_count,
            "expanded_trace_exact_count": expanded_count,
            "canonical_format_valid_count": canonical_format_count,
            "canonical_trace_exact_rate": canonical_count / valid if valid else None,
            "expanded_trace_exact_rate": expanded_count / valid if valid else None,
            "canonical_trace_exact_rate_all_requests": (
                canonical_count / expected if expected else None
            ),
            "expanded_trace_exact_rate_all_requests": (
                expanded_count / expected if expected else None
            ),
            "canonical_format_valid_rate": (
                canonical_format_count / valid if valid else None
            ),
        })
    else:
        exact_count = sum(row["state_exact"] for row in scores)
        position_sum = sum(row["state_position_accuracy"] for row in scores)
        summary.update({
            "state_exact_count": exact_count,
            "state_exact_rate": exact_count / valid if valid else None,
            "state_exact_rate_all_requests": exact_count / expected if expected else None,
            "mean_state_position_accuracy": (
                position_sum / valid if valid else None
            ),
            "mean_state_position_accuracy_all_requests": (
                position_sum / expected if expected else None
            ),
        })

    artifacts = {
        "summary": summary,
        "predictions": predictions,
        "scores": scores,
        "errors": errors,
    }
    if output_dir is not None:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        write_json(output_dir / "summary.json", summary)
        write_jsonl(output_dir / "predictions.jsonl", predictions)
        write_jsonl(output_dir / "scores.jsonl", scores)
        write_jsonl(output_dir / "response_errors.jsonl", errors)
    return artifacts


def build_combined_report(
    control_requests,
    control_scores,
    oracle_state_requests,
    oracle_state_scores,
    predicted_state_requests=None,
    predicted_state_scores=None,
    output_dir=None,
):
    """Combine decoupled metrics without re-coupling the model response schema."""
    control_score_by_case = {row["case_key"]: row for row in control_scores}
    oracle_score_by_id = {row["request_id"]: row for row in oracle_state_scores}
    predicted_request_by_parent = {
        row.get("parent_state_request_id"): row
        for row in (predicted_state_requests or [])
        if row.get("parent_state_request_id")
    }
    predicted_score_by_id = {
        row["request_id"]: row for row in (predicted_state_scores or [])
    }

    rows = []
    for state_request in oracle_state_requests:
        state_request_id = state_request["request_id"]
        case_key = state_request["case_key"]
        control_score = control_score_by_case.get(case_key)
        oracle_score = oracle_score_by_id.get(state_request_id)
        predicted_request = predicted_request_by_parent.get(state_request_id)
        predicted_score = (
            predicted_score_by_id.get(predicted_request["request_id"])
            if predicted_request is not None
            else None
        )
        cf_correct = bool(
            control_score and control_score.get("expanded_trace_exact")
        )
        oracle_state_correct = bool(
            oracle_score and oracle_score.get("state_exact")
        )
        predicted_state_correct = bool(
            predicted_score and predicted_score.get("state_exact")
        )
        rows.append({
            "case_key": case_key,
            "task_id": state_request["task_id"],
            "input_id": state_request["input_id"],
            "target_variable": state_request["target_variable"],
            "control_flow_expanded_exact": cf_correct,
            "oracle_cf_state_exact": oracle_state_correct,
            "predicted_cf_state_attempted": predicted_request is not None,
            "predicted_cf_state_exact": predicted_state_correct,
            "end_to_end_joint_exact": cf_correct and predicted_state_correct,
        })

    count = len(rows)
    control_case_count = len(control_requests)
    control_correct_count = sum(
        bool(control_score_by_case.get(row["case_key"], {}).get("expanded_trace_exact"))
        for row in control_requests
    )
    oracle_count = sum(row["oracle_cf_state_exact"] for row in rows)
    predicted_count = sum(row["predicted_cf_state_exact"] for row in rows)
    joint_count = sum(row["end_to_end_joint_exact"] for row in rows)
    cf_correct_rows = [row for row in rows if row["control_flow_expanded_exact"]]
    cf_wrong_rows = [row for row in rows if not row["control_flow_expanded_exact"]]

    summary = {
        "schema_version": COMBINED_SCHEMA_VERSION,
        "control_case_count": control_case_count,
        "state_task_count": count,
        "control_flow_expanded_exact_rate": (
            control_correct_count / control_case_count if control_case_count else None
        ),
        "oracle_cf_state_exact_rate": oracle_count / count if count else None,
        "predicted_cf_state_exact_rate": predicted_count / count if count else None,
        "state_error_propagation_gap": (
            (oracle_count - predicted_count) / count if count else None
        ),
        "end_to_end_joint_exact_rate": joint_count / count if count else None,
        "predicted_state_attempt_rate": (
            sum(row["predicted_cf_state_attempted"] for row in rows) / count
            if count
            else None
        ),
        "predicted_state_exact_given_cf_correct": (
            sum(row["predicted_cf_state_exact"] for row in cf_correct_rows)
            / len(cf_correct_rows)
            if cf_correct_rows
            else None
        ),
        "predicted_state_exact_given_cf_wrong": (
            sum(row["predicted_cf_state_exact"] for row in cf_wrong_rows)
            / len(cf_wrong_rows)
            if cf_wrong_rows
            else None
        ),
    }
    if output_dir is not None:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        write_json(output_dir / "summary.json", summary)
        write_jsonl(output_dir / "scores.jsonl", rows)
    return {"summary": summary, "scores": rows}


def main():
    parser = argparse.ArgumentParser(
        description="Evaluate decomposed control-flow and state responses."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    evaluate = subparsers.add_parser("evaluate")
    evaluate.add_argument("--requests", required=True)
    evaluate.add_argument("--oracles", required=True)
    evaluate.add_argument("--responses", required=True)
    evaluate.add_argument("--output-dir", required=True)

    report = subparsers.add_parser("report")
    report.add_argument("--control-requests", required=True)
    report.add_argument("--control-scores", required=True)
    report.add_argument("--oracle-state-requests", required=True)
    report.add_argument("--oracle-state-scores", required=True)
    report.add_argument("--predicted-state-requests")
    report.add_argument("--predicted-state-scores")
    report.add_argument("--output-dir", required=True)

    args = parser.parse_args()
    if args.command == "evaluate":
        result = evaluate_response_records(
            requests=read_jsonl(args.requests),
            oracles=read_jsonl(args.oracles),
            responses=read_jsonl(args.responses),
            output_dir=args.output_dir,
        )
    else:
        result = build_combined_report(
            control_requests=read_jsonl(args.control_requests),
            control_scores=read_jsonl(args.control_scores),
            oracle_state_requests=read_jsonl(args.oracle_state_requests),
            oracle_state_scores=read_jsonl(args.oracle_state_scores),
            predicted_state_requests=(
                read_jsonl(args.predicted_state_requests)
                if args.predicted_state_requests
                else None
            ),
            predicted_state_scores=(
                read_jsonl(args.predicted_state_scores)
                if args.predicted_state_scores
                else None
            ),
            output_dir=args.output_dir,
        )
    print(json.dumps(result["summary"], ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
