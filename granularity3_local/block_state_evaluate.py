import argparse
import json
from pathlib import Path

from granularity3_local.block_state_batch import read_jsonl
from granularity3_local.block_state_local import (
    flat_run_trace_length,
    flat_run_traces_equal,
)
from granularity3_local.oracle import write_json, write_jsonl


EVALUATION_SCHEMA_VERSION = "g3-block-state-evaluation-v2"


class ResponseValidationError(ValueError):
    """A model response cannot be compared safely with its requested batch."""


def _unique_by_batch_id(rows, label):
    indexed = {}
    for row_index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise ValueError(f"{label} row {row_index} must be an object")
        batch_id = row.get("batch_id")
        if not isinstance(batch_id, str) or not batch_id:
            raise ValueError(f"{label} row {row_index} has no valid batch_id")
        if batch_id in indexed:
            raise ValueError(f"duplicate {label} batch_id: {batch_id}")
        indexed[batch_id] = row
    return indexed


def parse_response_payload(value):
    """Parse the assistant's JSON object without repairing or guessing content."""
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        raise ResponseValidationError("response payload must be a JSON object or JSON string")
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as error:
        raise ResponseValidationError(
            f"response is not valid JSON: line {error.lineno} column {error.colno}"
        ) from error
    if not isinstance(payload, dict):
        raise ResponseValidationError("response JSON root must be an object")
    return payload


def response_payload_from_record(record):
    """Accept common offline response records while keeping batch metadata outside the model payload."""
    if "response" in record:
        return parse_response_payload(record["response"])
    if "raw_response" in record:
        return parse_response_payload(record["raw_response"])
    if "results" in record:
        return {"results": record["results"]}
    raise ResponseValidationError("response record needs response, raw_response, or results")


def _validate_changes(changes, trace_length, result_id):
    if not isinstance(changes, list):
        raise ResponseValidationError(f"{result_id}: changes must be a list")
    previous_key = None
    seen = set()
    normalized = []
    for index, row in enumerate(changes):
        if not isinstance(row, list) or len(row) != 4:
            raise ResponseValidationError(
                f"{result_id}: changes[{index}] must be [step, variable, before, after]"
            )
        step, variable, before, after = row
        if isinstance(step, bool) or not isinstance(step, int):
            raise ResponseValidationError(f"{result_id}: changes[{index}] step must be an integer")
        if step < 0 or step >= trace_length:
            raise ResponseValidationError(
                f"{result_id}: changes[{index}] step {step} is outside block_trace"
            )
        if not isinstance(variable, str) or not variable:
            raise ResponseValidationError(f"{result_id}: changes[{index}] variable must be a string")
        key = (step, variable)
        if key in seen:
            raise ResponseValidationError(f"{result_id}: duplicate change for step/variable {key}")
        if previous_key is not None and key < previous_key:
            raise ResponseValidationError(f"{result_id}: changes must be sorted by step and variable")
        seen.add(key)
        previous_key = key
        normalized.append([step, variable, before, after])
    return normalized


def validate_batch_response(request_record, payload):
    """Validate one model payload against the exact cases and blocks that were requested."""
    batch_id = request_record["batch_id"]
    request = request_record.get("request")
    if not isinstance(request, dict):
        raise ValueError(f"{batch_id}: request must be an object")
    expected_ids = [case["id"] for case in request.get("cases", [])]
    if len(expected_ids) != len(set(expected_ids)):
        raise ValueError(f"{batch_id}: request contains duplicate case ids")

    if set(payload) != {"results"}:
        raise ResponseValidationError(f"{batch_id}: response root must contain only results")
    results = payload["results"]
    if not isinstance(results, list):
        raise ResponseValidationError(f"{batch_id}: results must be a list")

    returned_ids = []
    normalized = []
    allowed_blocks = {row[0] for row in request.get("blocks", [])}
    for index, result in enumerate(results):
        if not isinstance(result, dict):
            raise ResponseValidationError(f"{batch_id}: results[{index}] must be an object")
        if set(result) != {"id", "block_trace", "changes"}:
            raise ResponseValidationError(
                f"{batch_id}: results[{index}] must contain only id, block_trace, and changes"
            )
        result_id = result["id"]
        if not isinstance(result_id, str):
            raise ResponseValidationError(f"{batch_id}: results[{index}].id must be a string")
        returned_ids.append(result_id)

        trace = result["block_trace"]
        if not isinstance(trace, list) or not trace:
            raise ResponseValidationError(f"{batch_id}/{result_id}: block_trace must be a non-empty list")
        for step, block_id in enumerate(trace):
            if not isinstance(block_id, str) or block_id not in allowed_blocks:
                raise ResponseValidationError(
                    f"{batch_id}/{result_id}: unknown block at step {step}: {block_id!r}"
                )
        changes = _validate_changes(result["changes"], len(trace), f"{batch_id}/{result_id}")
        normalized.append({"id": result_id, "block_trace": list(trace), "changes": changes})

    if returned_ids != expected_ids:
        missing = [item for item in expected_ids if item not in returned_ids]
        extra = [item for item in returned_ids if item not in expected_ids]
        duplicates = sorted({item for item in returned_ids if returned_ids.count(item) > 1})
        raise ResponseValidationError(
            f"{batch_id}: result ids/order differ from request; "
            f"missing={missing}, extra={extra}, duplicates={duplicates}, "
            f"expected={expected_ids}, returned={returned_ids}"
        )
    return {"batch_id": batch_id, "results": normalized}


def validate_single_case_response(request_record, payload):
    """Validate the direct local_answer.json shape used by one-case requests."""
    batch_id = request_record["batch_id"]
    request = request_record.get("request")
    if not isinstance(request, dict):
        raise ValueError(f"{batch_id}: request must be an object")
    cases = request.get("cases", [])
    if len(cases) != 1:
        raise ValueError(f"{batch_id}: single-case response requires exactly one request case")
    if set(payload) != {"block_trace", "changes"}:
        raise ResponseValidationError(
            f"{batch_id}: direct response must contain only block_trace and changes"
        )

    trace = payload["block_trace"]
    if not isinstance(trace, list) or not trace:
        raise ResponseValidationError(f"{batch_id}: block_trace must be a non-empty list")
    allowed_blocks = {row[0] for row in request.get("blocks", [])}
    for step, block_id in enumerate(trace):
        if not isinstance(block_id, str) or block_id not in allowed_blocks:
            raise ResponseValidationError(
                f"{batch_id}/{cases[0]['id']}: unknown block at step {step}: {block_id!r}"
            )
    changes = _validate_changes(payload["changes"], len(trace), f"{batch_id}/{cases[0]['id']}")
    return {
        "batch_id": batch_id,
        "results": [{
            "id": cases[0]["id"],
            "block_trace": list(trace),
            "changes": changes,
        }],
    }


def validate_flat_run_response(request_record, payload):
    """Validate the shallow [path, repeat_count] answer used for loop compression."""
    batch_id = request_record["batch_id"]
    request = request_record.get("request")
    if not isinstance(request, dict):
        raise ValueError(f"{batch_id}: request must be an object")
    cases = request.get("cases", [])
    if len(cases) != 1:
        raise ValueError(f"{batch_id}: flat-run response requires exactly one request case")
    if set(payload) != {"block_trace", "changes"}:
        raise ResponseValidationError(
            f"{batch_id}: flat-run response must contain only block_trace and changes"
        )

    trace = payload["block_trace"]
    if not isinstance(trace, list) or not trace:
        raise ResponseValidationError(f"{batch_id}: block_trace must be a non-empty list")
    allowed_blocks = {row[0] for row in request.get("blocks", [])}
    normalized_trace = []
    previous_path = None
    canonical_format_valid = True
    for run_index, row in enumerate(trace):
        if not isinstance(row, list) or len(row) != 2:
            raise ResponseValidationError(
                f"{batch_id}: block_trace[{run_index}] must be [path, repeat_count]"
            )
        path, repeat_count = row
        if not isinstance(path, str) or not path:
            raise ResponseValidationError(f"{batch_id}: block_trace[{run_index}] path must be a string")
        if isinstance(repeat_count, bool) or not isinstance(repeat_count, int) or repeat_count < 1:
            raise ResponseValidationError(
                f"{batch_id}: block_trace[{run_index}] repeat_count must be a positive integer"
            )
        path_blocks = path.split(">")
        if any(not block_id or block_id not in allowed_blocks for block_id in path_blocks):
            raise ResponseValidationError(
                f"{batch_id}: block_trace[{run_index}] contains an unknown block path: {path!r}"
            )
        if path == previous_path:
            canonical_format_valid = False
        previous_path = path
        normalized_trace.append([path, repeat_count])

    changes = _validate_changes(payload["changes"], len(trace), f"{batch_id}/{cases[0]['id']}")
    return {
        "batch_id": batch_id,
        "canonical_format_valid": canonical_format_valid,
        "results": [{
            "id": cases[0]["id"],
            "block_trace": normalized_trace,
            "changes": changes,
        }],
    }


def attach_and_validate_response(request_record, raw_response):
    """Attach the trusted request batch_id after parsing the model-owned response body."""
    payload = parse_response_payload(raw_response)
    if request_record.get("response_format") == "flat_runs":
        return validate_flat_run_response(request_record, payload)
    if request_record.get("response_format") == "single_case":
        return validate_single_case_response(request_record, payload)
    return validate_batch_response(request_record, payload)


def _first_difference(left, right):
    for index, (left_item, right_item) in enumerate(zip(left, right)):
        if left_item != right_item:
            return index
    if len(left) != len(right):
        return min(len(left), len(right))
    return None


def _expanded_trace_length(trace):
    if trace and all(isinstance(row, list) and len(row) == 2 for row in trace):
        try:
            return flat_run_trace_length(trace)
        except (TypeError, ValueError):
            pass
    return len(trace)


def _expanded_block_exact(predicted_trace, oracle_trace):
    predicted_is_flat = bool(predicted_trace) and all(
        isinstance(row, list) and len(row) == 2 for row in predicted_trace
    )
    oracle_is_flat = bool(oracle_trace) and all(
        isinstance(row, list) and len(row) == 2 for row in oracle_trace
    )
    if predicted_is_flat and oracle_is_flat:
        return flat_run_traces_equal(predicted_trace, oracle_trace)
    return predicted_trace == oracle_trace


def score_prediction(prediction, oracle_record):
    batch_id = prediction["batch_id"]
    if oracle_record.get("batch_id") != batch_id:
        raise ValueError(f"oracle batch_id does not match prediction: {batch_id}")
    oracle_results = oracle_record.get("results")
    if not isinstance(oracle_results, list):
        raise ValueError(f"{batch_id}: oracle results must be a list")
    oracle_by_id = {row["id"]: row for row in oracle_results}
    predicted_ids = [row["id"] for row in prediction["results"]]
    oracle_ids = [row["id"] for row in oracle_results]
    if predicted_ids != oracle_ids or len(oracle_by_id) != len(oracle_results):
        raise ValueError(f"{batch_id}: oracle ids/order do not match the request")

    case_scores = []
    canonical_format_valid = prediction.get("canonical_format_valid", True)
    for predicted in prediction["results"]:
        result_id = predicted["id"]
        oracle = oracle_by_id[result_id]
        canonical_block_exact = predicted["block_trace"] == oracle["block_trace"]
        expanded_block_exact = _expanded_block_exact(
            predicted["block_trace"], oracle["block_trace"]
        )
        changes_exact = predicted["changes"] == oracle["changes"]
        case_scores.append({
            "batch_id": batch_id,
            "id": result_id,
            "case_key": f"{batch_id.split('/', 1)[0]}/{result_id}",
            "canonical_format_valid": canonical_format_valid,
            "block_exact": canonical_block_exact,
            "canonical_block_exact": canonical_block_exact,
            "expanded_block_exact": expanded_block_exact,
            "changes_exact": changes_exact,
            "joint_exact": canonical_block_exact and changes_exact,
            "canonical_joint_exact": canonical_block_exact and changes_exact,
            "expanded_joint_exact": expanded_block_exact and changes_exact,
            "predicted_block_steps": _expanded_trace_length(predicted["block_trace"]),
            "oracle_block_steps": _expanded_trace_length(oracle["block_trace"]),
            "predicted_block_runs": len(predicted["block_trace"]),
            "oracle_block_runs": len(oracle["block_trace"]),
            "predicted_change_count": len(predicted["changes"]),
            "oracle_change_count": len(oracle["changes"]),
            "first_block_difference": _first_difference(
                predicted["block_trace"], oracle["block_trace"]
            ),
            "first_change_difference": _first_difference(predicted["changes"], oracle["changes"]),
        })
    count = len(case_scores)
    return case_scores, {
        "batch_id": batch_id,
        "status": "scored",
        "case_count": count,
        "canonical_format_valid_count": sum(
            row["canonical_format_valid"] for row in case_scores
        ),
        "block_exact_count": sum(row["block_exact"] for row in case_scores),
        "canonical_block_exact_count": sum(row["canonical_block_exact"] for row in case_scores),
        "expanded_block_exact_count": sum(row["expanded_block_exact"] for row in case_scores),
        "changes_exact_count": sum(row["changes_exact"] for row in case_scores),
        "joint_exact_count": sum(row["joint_exact"] for row in case_scores),
        "canonical_joint_exact_count": sum(row["canonical_joint_exact"] for row in case_scores),
        "expanded_joint_exact_count": sum(row["expanded_joint_exact"] for row in case_scores),
    }


def evaluate_response_records(model_batches, oracle_batches, response_records, output_dir=None):
    requests = _unique_by_batch_id(model_batches, "request")
    oracles = _unique_by_batch_id(oracle_batches, "oracle")
    if set(requests) != set(oracles):
        missing = sorted(set(requests) - set(oracles))
        extra = sorted(set(oracles) - set(requests))
        raise ValueError(f"request/oracle batch ids differ: missing_oracles={missing}, extra_oracles={extra}")

    response_rows = {}
    response_errors = []
    for row_index, record in enumerate(response_records, start=1):
        if not isinstance(record, dict):
            response_errors.append({
                "batch_id": None,
                "status": "invalid_response_record",
                "reason": f"response row {row_index} must be an object",
            })
            continue
        batch_id = record.get("batch_id")
        if not isinstance(batch_id, str) or not batch_id:
            response_errors.append({
                "batch_id": None,
                "status": "invalid_response_record",
                "reason": f"response row {row_index} has no valid batch_id",
            })
        elif batch_id not in requests:
            response_errors.append({
                "batch_id": batch_id,
                "status": "unknown_batch_id",
                "reason": "response batch_id is not present in model_batches",
            })
        elif batch_id in response_rows:
            response_errors.append({
                "batch_id": batch_id,
                "status": "duplicate_response",
                "reason": "more than one response record was supplied for this batch",
            })
        else:
            response_rows[batch_id] = record

    predictions = []
    case_scores = []
    batch_scores = []
    for batch_id, request_record in requests.items():
        response_record = response_rows.get(batch_id)
        if response_record is None:
            error = {
                "batch_id": batch_id,
                "status": "missing_response",
                "reason": "no response record was supplied for this batch",
            }
            response_errors.append(error)
            batch_scores.append(error)
            continue
        try:
            payload = response_payload_from_record(response_record)
            prediction = attach_and_validate_response(request_record, payload)
            scored_cases, batch_score = score_prediction(prediction, oracles[batch_id])
        except (ResponseValidationError, ValueError, KeyError, TypeError) as error:
            failure = {
                "batch_id": batch_id,
                "status": "invalid_response",
                "reason": str(error),
            }
            response_errors.append(failure)
            batch_scores.append(failure)
            continue
        predictions.append(prediction)
        case_scores.extend(scored_cases)
        batch_scores.append(batch_score)

    scored_count = len(case_scores)
    expected_case_count = sum(
        len(row["request"]["cases"]) for row in model_batches
    )
    canonical_block_exact_count = sum(
        row["canonical_block_exact"] for row in case_scores
    )
    canonical_format_valid_count = sum(
        row["canonical_format_valid"] for row in case_scores
    )
    expanded_block_exact_count = sum(
        row["expanded_block_exact"] for row in case_scores
    )
    changes_exact_count = sum(row["changes_exact"] for row in case_scores)
    canonical_joint_exact_count = sum(
        row["canonical_joint_exact"] for row in case_scores
    )
    expanded_joint_exact_count = sum(
        row["expanded_joint_exact"] for row in case_scores
    )
    summary = {
        "schema_version": EVALUATION_SCHEMA_VERSION,
        "expected_batch_count": len(requests),
        "response_record_count": len(response_records),
        "valid_batch_count": len(predictions),
        "invalid_or_missing_batch_count": len(requests) - len(predictions),
        "response_error_count": len(response_errors),
        "expected_case_count": expected_case_count,
        "scored_case_count": scored_count,
        "format_valid_rate": (
            scored_count / expected_case_count if expected_case_count else None
        ),
        "canonical_format_valid_count": canonical_format_valid_count,
        "canonical_format_valid_rate": (
            canonical_format_valid_count / scored_count if scored_count else None
        ),
        "canonical_format_valid_rate_all_cases": (
            canonical_format_valid_count / expected_case_count
            if expected_case_count else None
        ),
        "block_exact_count": canonical_block_exact_count,
        "canonical_block_exact_count": canonical_block_exact_count,
        "expanded_block_exact_count": expanded_block_exact_count,
        "changes_exact_count": changes_exact_count,
        "joint_exact_count": canonical_joint_exact_count,
        "canonical_joint_exact_count": canonical_joint_exact_count,
        "expanded_joint_exact_count": expanded_joint_exact_count,
        "block_exact_rate": (
            canonical_block_exact_count / scored_count if scored_count else None
        ),
        "canonical_block_exact_rate": (
            canonical_block_exact_count / scored_count if scored_count else None
        ),
        "expanded_block_exact_rate": (
            expanded_block_exact_count / scored_count if scored_count else None
        ),
        "changes_exact_rate": (
            changes_exact_count / scored_count if scored_count else None
        ),
        "joint_exact_rate": (
            canonical_joint_exact_count / scored_count if scored_count else None
        ),
        "canonical_joint_exact_rate": (
            canonical_joint_exact_count / scored_count if scored_count else None
        ),
        "expanded_joint_exact_rate": (
            expanded_joint_exact_count / scored_count if scored_count else None
        ),
        "canonical_block_exact_rate_all_cases": (
            canonical_block_exact_count / expected_case_count
            if expected_case_count else None
        ),
        "expanded_block_exact_rate_all_cases": (
            expanded_block_exact_count / expected_case_count
            if expected_case_count else None
        ),
        "changes_exact_rate_all_cases": (
            changes_exact_count / expected_case_count if expected_case_count else None
        ),
        "canonical_joint_exact_rate_all_cases": (
            canonical_joint_exact_count / expected_case_count
            if expected_case_count else None
        ),
        "expanded_joint_exact_rate_all_cases": (
            expanded_joint_exact_count / expected_case_count
            if expected_case_count else None
        ),
        "complete": len(predictions) == len(requests) and not response_errors,
    }
    artifacts = {
        "model_predictions": predictions,
        "case_scores": case_scores,
        "batch_scores": batch_scores,
        "response_errors": response_errors,
        "summary": summary,
    }
    if output_dir is not None:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        write_jsonl(output_dir / "model_predictions.jsonl", predictions)
        write_jsonl(output_dir / "case_scores.jsonl", case_scores)
        write_jsonl(output_dir / "batch_scores.jsonl", batch_scores)
        write_jsonl(output_dir / "response_errors.jsonl", response_errors)
        write_json(output_dir / "summary.json", summary)
    return artifacts


def main():
    parser = argparse.ArgumentParser(
        description="Validate batched block/state model responses and score them against local Oracle data."
    )
    parser.add_argument("--model-batches", required=True)
    parser.add_argument("--oracle-batches", required=True)
    parser.add_argument("--responses", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--allow-partial", action="store_true")
    args = parser.parse_args()

    artifacts = evaluate_response_records(
        read_jsonl(args.model_batches),
        read_jsonl(args.oracle_batches),
        read_jsonl(args.responses),
        args.output_dir,
    )
    print(json.dumps(artifacts["summary"], ensure_ascii=False, sort_keys=True))
    if not artifacts["summary"]["complete"] and not args.allow_partial:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
