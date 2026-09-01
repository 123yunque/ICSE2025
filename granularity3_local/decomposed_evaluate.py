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


EVALUATION_SCHEMA_VERSION = "g3-decomposed-evaluation-v2"
COMBINED_SCHEMA_VERSION = "g3-decomposed-combined-v2"


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


def _rate(numerator, denominator):
    return numerator / denominator if denominator else None


def _mean(values):
    values = list(values)
    return sum(values) / len(values) if values else None


def _state_length_bin(length):
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

    score_by_id = {row["request_id"]: row for row in scores}
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
        "format_valid_rate": _rate(valid, expected),
    }
    task_requests = {}
    for request in requests:
        task_requests.setdefault(request["task_id"], []).append(request)
    task_scores = []
    case_scores = []
    length_bin_scores = []
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
            "canonical_trace_exact_rate": _rate(canonical_count, valid),
            "expanded_trace_exact_rate": _rate(expanded_count, valid),
            "canonical_trace_exact_rate_all_requests": _rate(canonical_count, expected),
            "expanded_trace_exact_rate_all_requests": _rate(expanded_count, expected),
            "canonical_format_valid_rate": _rate(canonical_format_count, valid),
        })
        for task_id, task_rows in task_requests.items():
            task_metrics = [score_by_id.get(row["request_id"]) for row in task_rows]
            task_expected = len(task_rows)
            task_valid = sum(metric is not None for metric in task_metrics)
            task_canonical = sum(
                bool(metric and metric.get("canonical_trace_exact"))
                for metric in task_metrics
            )
            task_expanded = sum(
                bool(metric and metric.get("expanded_trace_exact"))
                for metric in task_metrics
            )
            task_scores.append({
                "task_id": task_id,
                "kind": kind,
                "expected_request_count": task_expected,
                "valid_response_count": task_valid,
                "canonical_trace_exact_rate_all_requests": _rate(
                    task_canonical, task_expected
                ),
                "expanded_trace_exact_rate_all_requests": _rate(
                    task_expanded, task_expected
                ),
            })
        summary.update({
            "task_count": len(task_scores),
            "task_macro_canonical_trace_exact_rate_all_requests": _mean(
                row["canonical_trace_exact_rate_all_requests"] for row in task_scores
            ),
            "task_macro_expanded_trace_exact_rate_all_requests": _mean(
                row["expanded_trace_exact_rate_all_requests"] for row in task_scores
            ),
        })
    else:
        exact_count = sum(row["state_exact"] for row in scores)
        position_sum = sum(row["state_position_accuracy"] for row in scores)
        summary.update({
            "state_exact_count": exact_count,
            "state_exact_rate": _rate(exact_count, valid),
            "state_exact_rate_all_requests": _rate(exact_count, expected),
            "mean_state_position_accuracy": _rate(position_sum, valid),
            "mean_state_position_accuracy_all_requests": _rate(
                position_sum, expected
            ),
        })

        for task_id, task_rows in task_requests.items():
            task_metrics = [score_by_id.get(row["request_id"]) for row in task_rows]
            task_expected = len(task_rows)
            task_valid = sum(metric is not None for metric in task_metrics)
            task_exact = sum(
                bool(metric and metric.get("state_exact")) for metric in task_metrics
            )
            task_position_sum = sum(
                metric.get("state_position_accuracy", 0.0) if metric else 0.0
                for metric in task_metrics
            )
            task_scores.append({
                "task_id": task_id,
                "kind": kind,
                "expected_request_count": task_expected,
                "valid_response_count": task_valid,
                "state_exact_rate_all_requests": _rate(task_exact, task_expected),
                "mean_state_position_accuracy_all_requests": _rate(
                    task_position_sum, task_expected
                ),
            })

        requests_by_case = {}
        for request in requests:
            requests_by_case.setdefault(request["case_key"], []).append(request)
        for case_key, case_rows in requests_by_case.items():
            case_metrics = [score_by_id.get(row["request_id"]) for row in case_rows]
            exact_count_for_case = sum(
                bool(metric and metric.get("state_exact")) for metric in case_metrics
            )
            position_sum_for_case = sum(
                metric.get("state_position_accuracy", 0.0) if metric else 0.0
                for metric in case_metrics
            )
            case_scores.append({
                "case_key": case_key,
                "task_id": case_rows[0]["task_id"],
                "input_id": case_rows[0]["input_id"],
                "kind": kind,
                "expected_variable_count": len(case_rows),
                "valid_variable_count": sum(
                    metric is not None for metric in case_metrics
                ),
                "exact_variable_count": exact_count_for_case,
                "all_variables_exact": exact_count_for_case == len(case_rows),
                "mean_state_position_accuracy": _rate(
                    position_sum_for_case, len(case_rows)
                ),
            })

        length_groups = {}
        for request in requests:
            oracle_length = len(oracle_by_id[request["request_id"]]["answer"]["states"])
            length_groups.setdefault(_state_length_bin(oracle_length), []).append(request)
        for label in ("2", "3-5", "6-10", "11-25", "26-100", ">100"):
            bin_rows = length_groups.get(label, [])
            if not bin_rows:
                continue
            bin_metrics = [score_by_id.get(row["request_id"]) for row in bin_rows]
            bin_exact = sum(
                bool(metric and metric.get("state_exact")) for metric in bin_metrics
            )
            length_bin_scores.append({
                "state_length_bin": label,
                "kind": kind,
                "expected_request_count": len(bin_rows),
                "valid_response_count": sum(
                    metric is not None for metric in bin_metrics
                ),
                "state_exact_rate_all_requests": _rate(bin_exact, len(bin_rows)),
            })

        case_exact_count = sum(row["all_variables_exact"] for row in case_scores)
        task_case_rates = []
        for task_id in task_requests:
            rows_for_task = [row for row in case_scores if row["task_id"] == task_id]
            task_case_rates.append(_rate(
                sum(row["all_variables_exact"] for row in rows_for_task),
                len(rows_for_task),
            ))
        summary.update({
            "state_case_count": len(case_scores),
            "state_case_exact_count": case_exact_count,
            "state_case_exact_rate_all_cases": _rate(
                case_exact_count, len(case_scores)
            ),
            "task_count": len(task_scores),
            "task_macro_state_exact_rate_all_requests": _mean(
                row["state_exact_rate_all_requests"] for row in task_scores
            ),
            "task_macro_state_position_accuracy_all_requests": _mean(
                row["mean_state_position_accuracy_all_requests"]
                for row in task_scores
            ),
            "task_macro_state_case_exact_rate_all_cases": _mean(task_case_rates),
            "predicted_state_count_shorter_than_oracle": sum(
                row["predicted_state_count"] < row["oracle_state_count"]
                for row in scores
            ),
            "predicted_state_count_equal_to_oracle": sum(
                row["predicted_state_count"] == row["oracle_state_count"]
                for row in scores
            ),
            "predicted_state_count_longer_than_oracle": sum(
                row["predicted_state_count"] > row["oracle_state_count"]
                for row in scores
            ),
        })

    artifacts = {
        "summary": summary,
        "predictions": predictions,
        "scores": scores,
        "case_scores": case_scores,
        "task_scores": task_scores,
        "length_bin_scores": length_bin_scores,
        "errors": errors,
    }
    if output_dir is not None:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        write_json(output_dir / "summary.json", summary)
        write_jsonl(output_dir / "predictions.jsonl", predictions)
        write_jsonl(output_dir / "scores.jsonl", scores)
        write_jsonl(output_dir / "case_scores.jsonl", case_scores)
        write_jsonl(output_dir / "task_scores.jsonl", task_scores)
        write_jsonl(output_dir / "length_bin_scores.jsonl", length_bin_scores)
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

    rows_by_case = {}
    for row in rows:
        rows_by_case.setdefault(row["case_key"], []).append(row)
    case_scores = []
    for case_key, case_rows in rows_by_case.items():
        expected_variables = len(case_rows)
        attempted_variables = sum(
            row["predicted_cf_state_attempted"] for row in case_rows
        )
        oracle_exact_variables = sum(
            row["oracle_cf_state_exact"] for row in case_rows
        )
        predicted_exact_variables = sum(
            row["predicted_cf_state_exact"] for row in case_rows
        )
        control_exact = case_rows[0]["control_flow_expanded_exact"]
        case_scores.append({
            "case_key": case_key,
            "task_id": case_rows[0]["task_id"],
            "input_id": case_rows[0]["input_id"],
            "expected_variable_count": expected_variables,
            "predicted_state_attempted_variable_count": attempted_variables,
            "control_flow_expanded_exact": control_exact,
            "oracle_cf_all_variables_exact": (
                oracle_exact_variables == expected_variables
            ),
            "predicted_cf_all_variables_exact": (
                predicted_exact_variables == expected_variables
            ),
            "end_to_end_case_joint_exact": (
                control_exact and predicted_exact_variables == expected_variables
            ),
        })

    task_ids = sorted({row["task_id"] for row in rows})
    task_scores = []
    for task_id in task_ids:
        task_variable_rows = [row for row in rows if row["task_id"] == task_id]
        task_case_rows = [row for row in case_scores if row["task_id"] == task_id]
        task_scores.append({
            "task_id": task_id,
            "state_variable_request_count": len(task_variable_rows),
            "state_case_count": len(task_case_rows),
            "oracle_cf_state_exact_rate": _rate(
                sum(row["oracle_cf_state_exact"] for row in task_variable_rows),
                len(task_variable_rows),
            ),
            "predicted_cf_state_exact_rate": _rate(
                sum(row["predicted_cf_state_exact"] for row in task_variable_rows),
                len(task_variable_rows),
            ),
            "end_to_end_joint_exact_rate": _rate(
                sum(row["end_to_end_joint_exact"] for row in task_variable_rows),
                len(task_variable_rows),
            ),
            "oracle_cf_state_case_exact_rate": _rate(
                sum(row["oracle_cf_all_variables_exact"] for row in task_case_rows),
                len(task_case_rows),
            ),
            "predicted_cf_state_case_exact_rate": _rate(
                sum(row["predicted_cf_all_variables_exact"] for row in task_case_rows),
                len(task_case_rows),
            ),
            "end_to_end_case_joint_exact_rate": _rate(
                sum(row["end_to_end_case_joint_exact"] for row in task_case_rows),
                len(task_case_rows),
            ),
        })

    state_case_count = len(case_scores)
    oracle_case_exact_count = sum(
        row["oracle_cf_all_variables_exact"] for row in case_scores
    )
    predicted_case_exact_count = sum(
        row["predicted_cf_all_variables_exact"] for row in case_scores
    )
    joint_case_exact_count = sum(
        row["end_to_end_case_joint_exact"] for row in case_scores
    )
    summary = {
        "schema_version": COMBINED_SCHEMA_VERSION,
        "control_case_count": control_case_count,
        "state_task_count": count,
        "state_case_count": state_case_count,
        "state_case_coverage_rate": _rate(state_case_count, control_case_count),
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
        "oracle_cf_state_case_exact_rate": _rate(
            oracle_case_exact_count, state_case_count
        ),
        "predicted_cf_state_case_exact_rate": _rate(
            predicted_case_exact_count, state_case_count
        ),
        "end_to_end_case_joint_exact_rate": _rate(
            joint_case_exact_count, state_case_count
        ),
        "state_task_macro_oracle_cf_state_exact_rate": _mean(
            row["oracle_cf_state_exact_rate"] for row in task_scores
        ),
        "state_task_macro_predicted_cf_state_exact_rate": _mean(
            row["predicted_cf_state_exact_rate"] for row in task_scores
        ),
        "state_task_macro_end_to_end_joint_exact_rate": _mean(
            row["end_to_end_joint_exact_rate"] for row in task_scores
        ),
        "state_task_macro_oracle_cf_state_case_exact_rate": _mean(
            row["oracle_cf_state_case_exact_rate"] for row in task_scores
        ),
        "state_task_macro_predicted_cf_state_case_exact_rate": _mean(
            row["predicted_cf_state_case_exact_rate"] for row in task_scores
        ),
        "state_task_macro_end_to_end_case_joint_exact_rate": _mean(
            row["end_to_end_case_joint_exact_rate"] for row in task_scores
        ),
    }
    if output_dir is not None:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        write_json(output_dir / "summary.json", summary)
        write_jsonl(output_dir / "scores.jsonl", rows)
        write_jsonl(output_dir / "case_scores.jsonl", case_scores)
        write_jsonl(output_dir / "task_scores.jsonl", task_scores)
    return {
        "summary": summary,
        "scores": rows,
        "case_scores": case_scores,
        "task_scores": task_scores,
    }


def legacy_state_sequence(changes, target_variable):
    """Project legacy run-level changes to a state sequence when representable."""
    variable_changes = [
        row for row in changes
        if isinstance(row, list) and len(row) == 4 and row[1] == target_variable
    ]
    if not variable_changes:
        return None
    states = [variable_changes[0][2]]
    chain_valid = True
    for _step, _variable, before, after in variable_changes:
        if before != states[-1]:
            chain_valid = False
        if after != states[-1]:
            states.append(after)
    return {"states": states, "chain_valid": chain_valid}


def build_legacy_compatibility_report(
    state_requests,
    state_oracles,
    legacy_oracles,
    legacy_predictions,
    legacy_case_scores=None,
    output_dir=None,
):
    """Compare the old joint model only where its run-level Oracle is compatible.

    The legacy task collapses all changes to one variable within a flat run to a
    single before/after pair.  The statement-level task retains every real
    change.  Direct comparison is therefore limited to request-variable pairs
    whose two Oracle projections are exactly equal.
    """
    oracle_by_request = _unique_by_request_id(state_oracles, "state oracle")
    legacy_oracle_by_case = {row["batch_id"]: row for row in legacy_oracles}
    legacy_prediction_by_case = {
        row["batch_id"]: row for row in legacy_predictions
    }
    legacy_control_score_by_case = {
        row["case_key"]: row for row in (legacy_case_scores or [])
    }
    rows = []
    incompatible = []
    for request in state_requests:
        request_id = request["request_id"]
        case_key = request["case_key"]
        variable = request["target_variable"]
        legacy_oracle_record = legacy_oracle_by_case.get(case_key)
        if legacy_oracle_record is None:
            incompatible.append({
                "request_id": request_id,
                "case_key": case_key,
                "task_id": request["task_id"],
                "target_variable": variable,
                "reason": "outside_legacy_cohort",
            })
            continue
        legacy_oracle_result = legacy_oracle_record["results"][0]
        projected_oracle = legacy_state_sequence(
            legacy_oracle_result.get("changes", []), variable
        )
        statement_answer = oracle_by_request[request_id]["answer"]
        if (
            projected_oracle is None
            or not projected_oracle["chain_valid"]
            or projected_oracle["states"] != statement_answer["states"]
        ):
            incompatible.append({
                "request_id": request_id,
                "case_key": case_key,
                "task_id": request["task_id"],
                "target_variable": variable,
                "reason": "oracle_projection_mismatch",
                "legacy_states": (
                    projected_oracle["states"] if projected_oracle else None
                ),
                "statement_states": statement_answer["states"],
            })
            continue

        legacy_prediction_record = legacy_prediction_by_case.get(case_key)
        projected_prediction = None
        if legacy_prediction_record is not None:
            projected_prediction = legacy_state_sequence(
                legacy_prediction_record["results"][0].get("changes", []),
                variable,
            )
        prediction_valid = bool(
            projected_prediction and projected_prediction["chain_valid"]
        )
        metric = (
            score_state(
                {"states": projected_prediction["states"]},
                statement_answer,
            )
            if prediction_valid
            else {
                "state_exact": False,
                "state_position_accuracy": 0.0,
                "matching_state_positions": 0,
                "predicted_state_count": 0,
                "oracle_state_count": len(statement_answer["states"]),
                "correct_prefix_length": 0,
                "first_state_difference": 0,
            }
        )
        control_score = legacy_control_score_by_case.get(case_key)
        control_exact = bool(
            control_score and control_score.get("expanded_block_exact")
        )
        rows.append({
            "request_id": request_id,
            "case_key": case_key,
            "task_id": request["task_id"],
            "input_id": request["input_id"],
            "target_variable": variable,
            "legacy_oracle_compatible": True,
            "legacy_prediction_available": legacy_prediction_record is not None,
            "legacy_prediction_chain_valid": prediction_valid,
            "legacy_control_flow_expanded_exact": control_exact,
            "legacy_end_to_end_joint_exact": control_exact and metric["state_exact"],
            **metric,
        })

    rows_by_case = {}
    for row in rows:
        rows_by_case.setdefault(row["case_key"], []).append(row)
    case_scores = []
    for case_key, case_rows in rows_by_case.items():
        case_scores.append({
            "case_key": case_key,
            "task_id": case_rows[0]["task_id"],
            "input_id": case_rows[0]["input_id"],
            "compatible_variable_count": len(case_rows),
            "legacy_control_flow_expanded_exact": case_rows[0][
                "legacy_control_flow_expanded_exact"
            ],
            "legacy_all_variables_state_exact": all(
                row["state_exact"] for row in case_rows
            ),
            "legacy_end_to_end_case_joint_exact": all(
                row["legacy_end_to_end_joint_exact"] for row in case_rows
            ),
        })

    compatible_count = len(rows)
    summary = {
        "schema_version": "g3-decomposed-legacy-compatibility-v1",
        "state_request_count": len(state_requests),
        "legacy_compatible_state_request_count": compatible_count,
        "legacy_incompatible_state_request_count": len(incompatible),
        "legacy_compatible_state_request_rate": _rate(
            compatible_count, len(state_requests)
        ),
        "legacy_state_exact_count": sum(row["state_exact"] for row in rows),
        "legacy_state_exact_rate_all_compatible_requests": _rate(
            sum(row["state_exact"] for row in rows), compatible_count
        ),
        "legacy_end_to_end_joint_exact_rate_all_compatible_requests": _rate(
            sum(row["legacy_end_to_end_joint_exact"] for row in rows),
            compatible_count,
        ),
        "legacy_compatible_case_count": len(case_scores),
        "legacy_state_case_exact_rate_all_compatible_cases": _rate(
            sum(row["legacy_all_variables_state_exact"] for row in case_scores),
            len(case_scores),
        ),
        "legacy_end_to_end_case_joint_exact_rate_all_compatible_cases": _rate(
            sum(
                row["legacy_end_to_end_case_joint_exact"] for row in case_scores
            ),
            len(case_scores),
        ),
    }
    if output_dir is not None:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        write_json(output_dir / "summary.json", summary)
        write_jsonl(output_dir / "scores.jsonl", rows)
        write_jsonl(output_dir / "case_scores.jsonl", case_scores)
        write_jsonl(output_dir / "incompatible.jsonl", incompatible)
    return {
        "summary": summary,
        "scores": rows,
        "case_scores": case_scores,
        "incompatible": incompatible,
    }


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

    legacy = subparsers.add_parser(
        "legacy-report",
        help="Compare the old joint baseline on statement-Oracle-compatible variables.",
    )
    legacy.add_argument("--state-requests", required=True)
    legacy.add_argument("--state-oracles", required=True)
    legacy.add_argument("--legacy-oracles", required=True)
    legacy.add_argument("--legacy-predictions", required=True)
    legacy.add_argument("--legacy-case-scores")
    legacy.add_argument("--output-dir", required=True)

    args = parser.parse_args()
    if args.command == "evaluate":
        result = evaluate_response_records(
            requests=read_jsonl(args.requests),
            oracles=read_jsonl(args.oracles),
            responses=read_jsonl(args.responses),
            output_dir=args.output_dir,
        )
    elif args.command == "report":
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
    else:
        result = build_legacy_compatibility_report(
            state_requests=read_jsonl(args.state_requests),
            state_oracles=read_jsonl(args.state_oracles),
            legacy_oracles=read_jsonl(args.legacy_oracles),
            legacy_predictions=read_jsonl(args.legacy_predictions),
            legacy_case_scores=(
                read_jsonl(args.legacy_case_scores)
                if args.legacy_case_scores
                else None
            ),
            output_dir=args.output_dir,
        )
    print(json.dumps(result["summary"], ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
