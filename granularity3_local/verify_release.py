import argparse
import json
from pathlib import Path


FORMAL_RUN_DIR = "block_state_api_full_gpt54_low_3557"
EXPECTED_LINE_COUNTS = {
    "selected_model_batches.jsonl": 3557,
    "selected_oracle_batches.jsonl": 3557,
    "model_responses.jsonl": 3557,
    "api_attempts.jsonl": 3558,
    "evaluation/case_scores.jsonl": 3471,
    "evaluation/response_errors.jsonl": 86,
}
EXPECTED_METRICS = {
    "expected_case_count": 3557,
    "response_record_count": 3557,
    "scored_case_count": 3471,
    "response_error_count": 86,
    "expanded_block_exact_count": 3357,
    "changes_exact_count": 2912,
    "expanded_joint_exact_count": 2898,
}
EXPECTED_CONFIG_FINGERPRINT = (
    "2f5bc8281cfa248fa0e3d9e17b393bbd01d2e05ab31e3d869fb6151ecbf96e59"
)


def read_json(path):
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def read_jsonl(path):
    records = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: {exc}") from exc
            if not isinstance(record, dict):
                raise ValueError(f"{path}:{line_number}: expected a JSON object")
            records.append(record)
    return records


def unique_ids(records, field, label, errors):
    values = [record.get(field) for record in records]
    missing = sum(value is None for value in values)
    if missing:
        errors.append(f"{label}: {missing} records have no {field}")
    unique = {value for value in values if value is not None}
    if len(unique) != len(values) - missing:
        errors.append(f"{label}: duplicate {field} values found")
    return unique


def verify(project_dir):
    run_dir = project_dir / FORMAL_RUN_DIR
    errors = []
    loaded = {}

    for relative, expected_count in EXPECTED_LINE_COUNTS.items():
        path = run_dir / relative
        if not path.is_file():
            errors.append(f"missing required artifact: {path}")
            continue
        try:
            records = read_jsonl(path)
        except (OSError, ValueError) as exc:
            errors.append(str(exc))
            continue
        loaded[relative] = records
        if len(records) != expected_count:
            errors.append(
                f"{relative}: expected {expected_count} records, found {len(records)}"
            )

    id_sets = {}
    for relative in (
        "selected_model_batches.jsonl",
        "selected_oracle_batches.jsonl",
        "model_responses.jsonl",
    ):
        if relative in loaded:
            id_sets[relative] = unique_ids(
                loaded[relative], "batch_id", relative, errors
            )
    if id_sets:
        first_name, first_ids = next(iter(id_sets.items()))
        for name, ids in id_sets.items():
            if ids != first_ids:
                errors.append(
                    f"batch_id mismatch: {first_name} and {name} differ"
                )

    expected_ids = id_sets.get("selected_model_batches.jsonl", set())
    scored = loaded.get("evaluation/case_scores.jsonl", [])
    rejected = loaded.get("evaluation/response_errors.jsonl", [])
    if expected_ids and scored and rejected:
        scored_ids = unique_ids(scored, "batch_id", "case_scores", errors)
        rejected_ids = unique_ids(rejected, "batch_id", "response_errors", errors)
        if scored_ids & rejected_ids:
            errors.append("scored and rejected batch_id sets overlap")
        if scored_ids | rejected_ids != expected_ids:
            errors.append("scored and rejected batch_id sets do not partition requests")

    summary_path = run_dir / "evaluation" / "summary.json"
    if not summary_path.is_file():
        errors.append(f"missing required artifact: {summary_path}")
    else:
        summary = read_json(summary_path)
        for key, expected in EXPECTED_METRICS.items():
            actual = summary.get(key)
            if actual != expected:
                errors.append(f"summary.{key}: expected {expected}, found {actual}")

    config_path = run_dir / "run_config.json"
    if not config_path.is_file():
        errors.append(f"missing required artifact: {config_path}")
    else:
        config = read_json(config_path)
        actual = config.get("run_config_fingerprint")
        if actual != EXPECTED_CONFIG_FINGERPRINT:
            errors.append(
                "run_config fingerprint mismatch: "
                f"expected {EXPECTED_CONFIG_FINGERPRINT}, found {actual}"
            )

    return errors


def main():
    parser = argparse.ArgumentParser(
        description="Verify the committed granularity-3 formal experiment artifacts."
    )
    parser.add_argument(
        "--project-dir",
        type=Path,
        default=Path(__file__).resolve().parent,
        help="granularity3_local directory (defaults to this script's directory)",
    )
    args = parser.parse_args()

    errors = verify(args.project_dir.resolve())
    if errors:
        print("Granularity-3 release verification: FAIL")
        for error in errors:
            print(f"- {error}")
        return 1

    print("Granularity-3 release verification: PASS")
    print("- 3557 requests, Oracle answers, and final responses match by batch_id")
    print("- 3471 scored cases + 86 rejected responses partition all requests")
    print("- 3558 API attempts and the formal configuration fingerprint match")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
