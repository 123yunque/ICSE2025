import json
import tempfile
import unittest
from pathlib import Path

from granularity3_local.block_state_batch import build_dataset_batches, extract_static_context
from granularity3_local.block_state_local import prepare_local_case
from granularity3_local.oracle import write_jsonl


class BlockStateBatchTests(unittest.TestCase):
    SOURCE = """\
def add_one(x):
    y = x + 1
    return y
"""

    def test_static_context_is_minimal_and_transitive(self):
        source = """\
import math
import re
OFFSET = 1
UNUSED = 9

def helper(x):
    return math.floor(x) + OFFSET

def unused(x):
    return re.escape(x)

def target(x):
    return helper(x)
"""
        context = extract_static_context(source, "target")
        self.assertIn("import math", context)
        self.assertIn("OFFSET = 1", context)
        self.assertIn("def helper", context)
        self.assertNotIn("import re", context)
        self.assertNotIn("UNUSED", context)
        self.assertNotIn("def unused", context)
        self.assertNotIn("def target", context)

    def test_cases_share_static_input_and_answers_stay_isolated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dataset = root / "dataset"
            task = dataset / "task_1"
            task.mkdir(parents=True)
            (task / "code.py").write_text(self.SOURCE, encoding="utf-8")
            local = root / "local"
            records = []
            for index in range(1, 4):
                input_id = f"input_{index}"
                case_dir = local / "cases" / "task_1" / input_id
                result = prepare_local_case(
                    source=self.SOURCE,
                    function_name="add_one",
                    input_text=f"[{index}]",
                    task_id="task_1",
                    input_id=input_id,
                    output_dir=case_dir,
                )
                manifest = result["manifest"]
                records.append({
                    "case_key": f"task_1/{input_id}",
                    "task_id": "task_1",
                    "input_id": input_id,
                    "function": "add_one",
                    "status": "success",
                    "event_count": manifest["event_count"],
                    "local_answer_chars": manifest["local_answer_chars"],
                    "model_input_chars": manifest["model_input_chars"],
                })
            write_jsonl(local / "case_records.jsonl", records)

            result = build_dataset_batches(dataset, local, root / "batches", max_cases=2)
            self.assertEqual(result["summary"]["eligible_case_count"], 3)
            self.assertEqual(result["summary"]["batch_count"], 2)
            first = result["model_batches"][0]["request"]
            self.assertEqual(set(first), {"fn", "blocks", "cases"})
            self.assertEqual([row["id"] for row in first["cases"]], ["input_1", "input_2"])
            visible = json.dumps(first, ensure_ascii=False)
            self.assertNotIn("block_trace", visible)
            self.assertNotIn("changes", visible)
            answers = result["answer_batches"][0]["results"]
            self.assertEqual([row["id"] for row in answers], ["input_1", "input_2"])
            self.assertIn("block_trace", answers[0])
            self.assertTrue(result["summary"]["answer_isolation"])
            self.assertLess(
                result["summary"]["batched_request_chars"],
                result["summary"]["unbatched_request_chars"],
            )

            single_result = build_dataset_batches(
                dataset,
                local,
                root / "single_case_batches",
                one_case_per_request=True,
            )
            self.assertEqual(single_result["summary"]["request_mode"], "per_case")
            self.assertEqual(single_result["summary"]["batch_count"], 3)
            self.assertEqual(
                [row["response_format"] for row in single_result["model_batches"]],
                ["single_case", "single_case", "single_case"],
            )
            self.assertEqual(
                [row["case_count"] for row in single_result["manifests"]],
                [1, 1, 1],
            )


if __name__ == "__main__":
    unittest.main()
