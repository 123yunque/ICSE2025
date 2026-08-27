import json
import tempfile
import unittest
from pathlib import Path

from granularity3_local.block_state_local import (
    build_flat_run_answer,
    build_local_answer,
    compact_value,
    expand_flat_run_trace,
    expand_value,
    flat_run_trace_length,
    flat_run_traces_equal,
    parse_dataset_args,
    prepare_local_case,
)
from granularity3_local.state import canonicalize


class BlockStateLocalTests(unittest.TestCase):
    SOURCE = """\
def f(n):
    total = 0
    while n > 0:
        total += n
        n -= 1
    return total
"""

    def test_dataset_outer_list_is_positional_arguments(self):
        self.assertEqual(parse_dataset_args("[2, 'x']"), (2, "x"))
        self.assertEqual(parse_dataset_args("([1, 2],)"), ([1, 2],))
        self.assertEqual(parse_dataset_args("3"), (3,))

    def test_compact_values_round_trip(self):
        values = [
            canonicalize([1, (2,)]),
            canonicalize({"x": [1, 2]}),
            canonicalize({1, 2}),
            {"$undefined": True},
        ]
        for value in values:
            self.assertEqual(expand_value(compact_value(value)), value)

    def test_local_case_contains_only_trace_and_sparse_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = prepare_local_case(
                source=self.SOURCE,
                function_name="f",
                input_text="[2]",
                task_id="task_test",
                input_id="input_1",
                output_dir=root,
            )
            expected_trace = ["B001", "B002", "B003", "B002", "B003", "B002", "B004"]
            expected_changes = [
                [0, "total", {"$u": 1}, 0],
                [2, "n", 2, 1],
                [2, "total", 0, 2],
                [4, "n", 1, 0],
                [4, "total", 2, 3],
            ]
            self.assertEqual(result["local_answer"], {
                "block_trace": expected_trace,
                "changes": expected_changes,
            })
            self.assertEqual(set(result["local_answer"]), {"block_trace", "changes"})
            self.assertEqual(result["model_input"]["fn"], "f(n)")
            self.assertEqual(result["model_input"]["args"], [2])
            visible = json.dumps(result["model_input"], ensure_ascii=False)
            for forbidden in ("pre_state", "state_before", "state_after", "state_delta", "occurrence"):
                self.assertNotIn(forbidden, visible)
            self.assertTrue((root / "oracle" / "events.jsonl").exists())
            self.assertTrue((root / "oracle" / "case.json").exists())
            self.assertTrue(result["manifest"]["raw_oracle_preserved"])
            self.assertTrue(result["manifest"]["runtime_state_excluded_from_model_input"])

    def test_build_local_answer_omits_unchanged_steps(self):
        events = [
            {"block_id": "B001", "state_delta": {}},
            {
                "block_id": "B002",
                "state_delta": {"x": {"before": 1, "after": 2}},
            },
        ]
        self.assertEqual(build_local_answer(events), {
            "block_trace": ["B001", "B002"],
            "changes": [[1, "x", 1, 2]],
        })

    def test_flat_runs_compress_loop_and_preserve_trace(self):
        with tempfile.TemporaryDirectory() as directory:
            result = prepare_local_case(
                source=self.SOURCE,
                function_name="f",
                input_text="[2]",
                task_id="task_test",
                input_id="input_1",
                output_dir=directory,
            )
        flat = build_flat_run_answer(result["local_answer"], result["model_input"]["blocks"])
        self.assertEqual(flat, {
            "block_trace": [
                ["B001", 1],
                ["B002>B003", 2],
                ["B002", 1],
                ["B004", 1],
            ],
            "changes": [
                [0, "total", {"$u": 1}, 0],
                [1, "n", 2, 0],
                [1, "total", 0, 3],
            ],
        })
        self.assertEqual(
            expand_flat_run_trace(flat["block_trace"]),
            result["local_answer"]["block_trace"],
        )

    def test_flat_run_semantic_comparison_does_not_expand_long_loops(self):
        repeat_count = 10 ** 30
        left = [["B002>B003", repeat_count]]
        differently_segmented = [["B002>B003>B002>B003", repeat_count // 2]]
        standalone = [["B002", 1], ["B003", 1], ["B004", 1]]
        grouped = [["B002>B003>B004", 1]]

        self.assertEqual(flat_run_trace_length(left), 2 * repeat_count)
        self.assertTrue(flat_run_traces_equal(left, differently_segmented))
        self.assertTrue(flat_run_traces_equal(standalone, grouped))
        self.assertFalse(flat_run_traces_equal(left, [["B002>B004", repeat_count]]))
        self.assertFalse(flat_run_traces_equal(left, [["B002>B003", repeat_count - 1]]))


if __name__ == "__main__":
    unittest.main()
