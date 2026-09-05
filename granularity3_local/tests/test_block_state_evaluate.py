import json
import tempfile
import unittest
from pathlib import Path

from granularity3_local.block_state_evaluate import (
    ResponseValidationError,
    attach_and_validate_response,
    evaluate_response_records,
)


class BlockStateEvaluateTests(unittest.TestCase):
    def setUp(self):
        self.request = {
            "batch_id": "task_1/batch_1",
            "request": {
                "fn": "f(x)",
                "blocks": [
                    ["B001", "y = x + 1", [["fallthrough", "B002"]]],
                    ["B002", "return y", [["return", None]]],
                ],
                "cases": [
                    {"id": "input_1", "args": [1]},
                    {"id": "input_2", "args": [2]},
                ],
            },
        }
        self.oracle = {
            "batch_id": "task_1/batch_1",
            "results": [
                {
                    "id": "input_1",
                    "block_trace": ["B001", "B002"],
                    "changes": [[0, "y", {"$u": 1}, 2]],
                },
                {
                    "id": "input_2",
                    "block_trace": ["B001", "B002"],
                    "changes": [[0, "y", {"$u": 1}, 3]],
                },
            ],
        }

    def test_batch_id_is_attached_from_request_and_exact_response_scores(self):
        raw = json.dumps({"results": self.oracle["results"]})
        prediction = attach_and_validate_response(self.request, raw)
        self.assertEqual(prediction["batch_id"], "task_1/batch_1")

        with tempfile.TemporaryDirectory() as directory:
            artifacts = evaluate_response_records(
                [self.request],
                [self.oracle],
                [{"batch_id": "task_1/batch_1", "raw_response": raw}],
                directory,
            )
            self.assertTrue(artifacts["summary"]["complete"])
            self.assertEqual(artifacts["summary"]["joint_exact_rate"], 1.0)
            self.assertTrue((Path(directory) / "model_predictions.jsonl").exists())
            self.assertTrue((Path(directory) / "case_scores.jsonl").exists())
            self.assertTrue((Path(directory) / "summary.json").exists())

    def test_single_case_direct_response_matches_local_answer_shape(self):
        request = {
            "batch_id": "task_1/input_1",
            "response_format": "single_case",
            "request": {
                "fn": "f(x)",
                "blocks": self.request["request"]["blocks"],
                "cases": [{"id": "input_1", "args": [1]}],
            },
        }
        local_answer = self.oracle["results"][0]
        direct = {
            "block_trace": local_answer["block_trace"],
            "changes": local_answer["changes"],
        }
        prediction = attach_and_validate_response(request, direct)
        self.assertEqual(prediction["results"][0]["id"], "input_1")
        self.assertEqual(prediction["results"][0]["block_trace"], direct["block_trace"])
        self.assertEqual(prediction["results"][0]["changes"], direct["changes"])

    def test_flat_run_direct_response_is_valid_and_scores_exactly(self):
        request = {
            "batch_id": "task_1/input_1",
            "response_format": "flat_runs",
            "request": {
                "fn": "f(x)",
                "blocks": self.request["request"]["blocks"],
                "cases": [{"id": "input_1", "args": [1]}],
            },
        }
        direct = {
            "block_trace": [["B001>B002", 3], ["B002", 1]],
            "changes": [[0, "y", {"$u": 1}, 2]],
        }
        oracle = {
            "batch_id": "task_1/input_1",
            "response_format": "flat_runs",
            "results": [{"id": "input_1", **direct}],
        }
        artifacts = evaluate_response_records(
            [request],
            [oracle],
            [{"batch_id": "task_1/input_1", "response": direct}],
        )
        self.assertTrue(artifacts["summary"]["complete"])
        self.assertEqual(artifacts["summary"]["joint_exact_rate"], 1.0)
        self.assertEqual(artifacts["summary"]["expanded_block_exact_rate"], 1.0)
        self.assertEqual(artifacts["case_scores"][0]["oracle_block_steps"], 7)

    def test_flat_run_expanded_sequence_matches_without_canonical_segmentation(self):
        request = {
            "batch_id": "task_1/input_1",
            "response_format": "flat_runs",
            "request": {
                "fn": "f(x)",
                "blocks": self.request["request"]["blocks"],
                "cases": [{"id": "input_1", "args": [1]}],
            },
        }
        oracle = {
            "batch_id": "task_1/input_1",
            "response_format": "flat_runs",
            "results": [{
                "id": "input_1",
                "block_trace": [["B001", 1], ["B002", 1]],
                "changes": [],
            }],
        }
        direct = {
            "block_trace": [["B001>B002", 1]],
            "changes": [],
        }
        artifacts = evaluate_response_records(
            [request],
            [oracle],
            [{"batch_id": "task_1/input_1", "response": direct}],
        )
        score = artifacts["case_scores"][0]
        self.assertFalse(score["canonical_block_exact"])
        self.assertTrue(score["expanded_block_exact"])
        self.assertEqual(artifacts["summary"]["canonical_block_exact_rate"], 0.0)
        self.assertEqual(artifacts["summary"]["expanded_block_exact_rate"], 1.0)

    def test_adjacent_identical_paths_are_scored_but_marked_noncanonical(self):
        request = {
            "batch_id": "task_1/input_1",
            "response_format": "flat_runs",
            "request": {
                "fn": "f(x)",
                "blocks": self.request["request"]["blocks"],
                "cases": [{"id": "input_1", "args": [1]}],
            },
        }
        oracle = {
            "batch_id": "task_1/input_1",
            "response_format": "flat_runs",
            "results": [{
                "id": "input_1",
                "block_trace": [["B001", 2], ["B002", 1]],
                "changes": [],
            }],
        }
        direct = {
            "block_trace": [["B001", 1], ["B001", 1], ["B002", 1]],
            "changes": [],
        }
        artifacts = evaluate_response_records(
            [request],
            [oracle],
            [{"batch_id": "task_1/input_1", "response": direct}],
        )
        score = artifacts["case_scores"][0]
        self.assertFalse(score["canonical_format_valid"])
        self.assertFalse(score["canonical_block_exact"])
        self.assertTrue(score["expanded_block_exact"])
        self.assertEqual(artifacts["summary"]["format_valid_rate"], 1.0)
        self.assertEqual(artifacts["summary"]["canonical_format_valid_rate"], 0.0)

    def test_wrong_prediction_is_valid_but_scores_inexact(self):
        results = json.loads(json.dumps(self.oracle["results"]))
        results[0]["block_trace"] = ["B002"]
        results[0]["changes"] = []
        artifacts = evaluate_response_records(
            [self.request],
            [self.oracle],
            [{"batch_id": "task_1/batch_1", "response": {"results": results}}],
        )
        self.assertTrue(artifacts["summary"]["complete"])
        self.assertEqual(artifacts["summary"]["joint_exact_count"], 1)
        self.assertEqual(artifacts["case_scores"][0]["first_block_difference"], 0)

    def test_missing_duplicate_or_reordered_ids_are_rejected(self):
        reordered = list(reversed(self.oracle["results"]))
        with self.assertRaises(ResponseValidationError):
            attach_and_validate_response(self.request, {"results": reordered})

        duplicate = [self.oracle["results"][0], self.oracle["results"][0]]
        with self.assertRaises(ResponseValidationError):
            attach_and_validate_response(self.request, {"results": duplicate})

        missing = [self.oracle["results"][0]]
        with self.assertRaises(ResponseValidationError):
            attach_and_validate_response(self.request, {"results": missing})

    def test_malformed_trace_and_changes_are_rejected(self):
        unknown_block = json.loads(json.dumps(self.oracle["results"]))
        unknown_block[0]["block_trace"][0] = "B999"
        with self.assertRaises(ResponseValidationError):
            attach_and_validate_response(self.request, {"results": unknown_block})

        invalid_step = json.loads(json.dumps(self.oracle["results"]))
        invalid_step[0]["changes"][0][0] = 9
        with self.assertRaises(ResponseValidationError):
            attach_and_validate_response(self.request, {"results": invalid_step})

        unsorted = json.loads(json.dumps(self.oracle["results"]))
        unsorted[0]["changes"] = [[1, "z", 0, 1], [0, "y", 1, 2]]
        with self.assertRaises(ResponseValidationError):
            attach_and_validate_response(self.request, {"results": unsorted})

    def test_missing_response_is_recorded_and_not_scored(self):
        artifacts = evaluate_response_records([self.request], [self.oracle], [])
        self.assertFalse(artifacts["summary"]["complete"])
        self.assertEqual(artifacts["summary"]["scored_case_count"], 0)
        self.assertEqual(artifacts["summary"]["format_valid_rate"], 0.0)
        self.assertEqual(
            artifacts["summary"]["expanded_block_exact_rate_all_cases"],
            0.0,
        )
        self.assertEqual(artifacts["response_errors"][0]["status"], "missing_response")


if __name__ == "__main__":
    unittest.main()
