import tempfile
import unittest
from pathlib import Path

from granularity3_local.block_state_canary import (
    _load_gate_case_keys,
    check_rollout_gates,
    compare_canary,
    select_full_rollout_cases,
    select_stratified_case_keys,
)
from granularity3_local.oracle import write_json, write_jsonl


class BlockStateCanaryTests(unittest.TestCase):
    def test_gate_can_load_all_case_keys_from_model_batches(self):
        with tempfile.TemporaryDirectory() as directory:
            model_batches = Path(directory) / "model_batches.jsonl"
            write_jsonl(model_batches, [
                {"batch_id": "task_1/input_1"},
                {"batch_id": "task_1/input_2"},
            ])
            self.assertEqual(
                _load_gate_case_keys(model_batches_path=model_batches),
                ["task_1/input_1", "task_1/input_2"],
            )

    def test_full_rollout_selection_spans_static_loop_complexity(self):
        models = []
        for task_number in range(1, 7):
            if task_number <= 2:
                blocks = [["B001", "return x", [["return", None]]]]
            elif task_number <= 4:
                blocks = [["B001", "for x in xs", [["loop_body", "B002"]]]]
            else:
                blocks = [
                    ["B001", "for x in xs", [["loop_body", "B002"]]],
                    ["B002", "while y", [["loop_body", "B003"]]],
                ]
            models.append({
                "batch_id": f"task_{task_number}/batch_1",
                "request": {
                    "blocks": blocks,
                    "cases": [
                        {"id": "input_1", "args": [1]},
                        {"id": "input_2", "args": [2]},
                    ],
                },
            })
        selection, task_count, strata = select_full_rollout_cases(models, sample_size=3)
        self.assertEqual(task_count, 6)
        self.assertEqual(strata, {"0_loops": 2, "1_loop": 2, "2plus_loops": 2})
        self.assertEqual(len(selection), 3)
        self.assertEqual(
            {row["complexity"] for row in selection},
            {"0_loops", "1_loop", "2plus_loops"},
        )
        self.assertEqual(len({row["task_id"] for row in selection}), 3)

    def test_selection_spans_each_task_latency_range(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "evaluation").mkdir(parents=True)
            models = []
            attempts = []
            scores = []
            for task_id in ("task_1", "task_2"):
                for index in range(1, 5):
                    case_key = f"{task_id}/input_{index}"
                    models.append({"batch_id": case_key, "request": {"cases": []}})
                    attempts.append({
                        "batch_id": case_key,
                        "status": "received",
                        "elapsed_seconds": index,
                    })
                    scores.append({
                        "case_key": case_key,
                        "expanded_block_exact": index % 2 == 0,
                        "changes_exact": True,
                    })
            write_jsonl(root / "selected_model_batches.jsonl", models)
            write_jsonl(root / "api_attempts.jsonl", attempts)
            write_jsonl(root / "evaluation" / "case_scores.jsonl", scores)
            selection = select_stratified_case_keys(root, sample_size=4)
            self.assertEqual(
                [row["case_key"] for row in selection],
                ["task_1/input_1", "task_1/input_4", "task_2/input_1", "task_2/input_4"],
            )

    def test_comparison_applies_quality_token_latency_and_provider_gates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline = root / "baseline"
            candidate = root / "candidate"
            case_keys = [f"task_1/input_{index}" for index in range(1, 5)]
            baseline_attempts = []
            candidate_attempts = []
            baseline_scores = []
            candidate_scores = []
            for index, case_key in enumerate(case_keys, start=1):
                baseline_attempts.append({
                    "batch_id": case_key,
                    "status": "received",
                    "elapsed_seconds": 100 + index,
                    "completion_tokens": 1000,
                    "prompt_tokens": 100,
                    "total_tokens": 1100,
                    "validation": "valid",
                })
                candidate_attempts.append({
                    "batch_id": case_key,
                    "status": "received",
                    "elapsed_seconds": 40 + index,
                    "completion_tokens": 400,
                    "prompt_tokens": 100,
                    "total_tokens": 500,
                    "reasoning_tokens": 0,
                    "reasoning_tokens_reported": True,
                    "validation": "valid",
                })
                baseline_scores.append({
                    "case_key": case_key,
                    "expanded_block_exact": index <= 3,
                    "changes_exact": True,
                    "canonical_joint_exact": index <= 2,
                })
                candidate_scores.append({
                    "case_key": case_key,
                    "expanded_block_exact": index <= 3,
                    "changes_exact": True,
                    "canonical_joint_exact": index <= 2,
                })
            for run_dir, attempts, scores in (
                (baseline, baseline_attempts, baseline_scores),
                (candidate, candidate_attempts, candidate_scores),
            ):
                (run_dir / "evaluation").mkdir(parents=True)
                write_jsonl(run_dir / "api_attempts.jsonl", attempts)
                write_jsonl(run_dir / "evaluation" / "case_scores.jsonl", scores)
            comparison = compare_canary(baseline, candidate, case_keys)
            self.assertTrue(comparison["all_criteria_pass"])
            self.assertAlmostEqual(
                comparison["deltas"]["completion_token_reduction"],
                0.6,
            )

    def test_rollout_gate_applies_absolute_midterm_thresholds(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            (run_dir / "evaluation").mkdir(parents=True)
            case_keys = [f"task_{index}/input_1" for index in range(1, 5)]
            attempts = []
            scores = []
            for index, case_key in enumerate(case_keys, start=1):
                attempts.append({
                    "batch_id": case_key,
                    "status": "received",
                    "elapsed_seconds": 10 + index,
                    "completion_tokens": 1000,
                    "finish_reason": "stop",
                    "validation": "valid",
                })
                scores.append({
                    "case_key": case_key,
                    "expanded_block_exact": index <= 3,
                    "changes_exact": True,
                    "canonical_joint_exact": index <= 2,
                })
            write_jsonl(run_dir / "api_attempts.jsonl", attempts)
            write_jsonl(run_dir / "evaluation" / "case_scores.jsonl", scores)
            write_json(run_dir / "run_config.json", {
                "generation": {"max_completion_tokens": 8192}
            })
            result = check_rollout_gates(run_dir, case_keys)
            self.assertTrue(result["all_criteria_pass"])
            self.assertEqual(result["metrics"]["format_valid_rate"], 1.0)
            self.assertEqual(result["metrics"]["completion_cap_hit_count"], 0)


if __name__ == "__main__":
    unittest.main()
