import tempfile
import unittest
from pathlib import Path

from granularity3_local.block_state_dataset import run_dataset


class BlockStateDatasetTests(unittest.TestCase):
    def test_dataset_run_isolated_and_resumable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dataset = root / "dataset"
            task = dataset / "task_1"
            task.mkdir(parents=True)
            (task / "code.py").write_text(
                "def add_one(x):\n    y = x + 1\n    return y\n",
                encoding="utf-8",
            )
            (task / "code_inputs.txt").write_text("[1]\n[2]\n", encoding="utf-8")
            (task / "meta.json").write_text(
                '{"function": "add_one", "test_list": ["assert add_one(1) == 2"]}',
                encoding="utf-8",
            )
            output = root / "output"
            first = run_dataset(dataset, output, workers=2, timeout_seconds=5)
            self.assertEqual(first["planned_case_count"], 2)
            self.assertEqual(first["successful_case_count"], 2)
            self.assertEqual(first["failed_cases"], [])
            self.assertTrue((output / "cases" / "task_1" / "input_1" / "local_answer.json").exists())

            second = run_dataset(dataset, output, workers=2, timeout_seconds=5, resume=True)
            self.assertEqual(second["successful_case_count"], 2)
            self.assertEqual(second["config"]["pending_case_count"], 0)


if __name__ == "__main__":
    unittest.main()
