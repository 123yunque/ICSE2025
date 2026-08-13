import json
import tempfile
import unittest
from pathlib import Path

from granularity3_local.dataset_pipeline import run_dataset_pipeline


class DatasetPipelineTests(unittest.TestCase):
    def make_task(self, root, task_id, source, inputs, test_name="f"):
        task = Path(root) / task_id
        task.mkdir()
        (task / "code.py").write_text(source, encoding="utf-8")
        (task / "code_inputs.txt").write_text("\n".join(inputs) + "\n", encoding="utf-8")
        (task / f"{task_id}.json").write_text(
            json.dumps({"test_list": [f"assert {test_name}(*()) is not None"]}), encoding="utf-8"
        )

    def test_supported_and_unsupported_tasks(self):
        with tempfile.TemporaryDirectory() as directory:
            dataset = Path(directory) / "dataset"
            output = Path(directory) / "output"
            dataset.mkdir()
            self.make_task(dataset, "task_1", "def f(n):\n    return n+1\n", ["(1,)", "(2,)"])
            self.make_task(
                dataset,
                "task_2",
                "def f(xs):\n    for x in xs:\n        break\n    return 0\n",
                ["([1],)"],
            )
            summary = run_dataset_pipeline(dataset, output, inputs_per_task=2, workers=2)
            self.assertEqual(summary["task_count"], 2)
            self.assertEqual(summary["supported_task_count"], 1)
            self.assertEqual(summary["planned_case_count"], 2)
            self.assertEqual(summary["successful_case_count"], 2)
            self.assertEqual(summary["preflight_statuses"]["unsupported_jump"], 1)
            self.assertTrue((output / "summary.json").exists())


if __name__ == "__main__":
    unittest.main()
