import tempfile
import unittest
from pathlib import Path

from granularity3_local.legacy.pipeline import run_local_pipeline


class PipelineTests(unittest.TestCase):
    def test_end_to_end_pre_llm_pipeline(self):
        with tempfile.TemporaryDirectory() as directory:
            task_dir = Path(directory) / "task_1"
            output_dir = Path(directory) / "output"
            task_dir.mkdir()
            (task_dir / "code.py").write_text(
                "def f(n):\n    total = 0\n    while n > 0:\n        total += n\n        n -= 1\n    return total\n",
                encoding="utf-8",
            )
            (task_dir / "code_inputs.txt").write_text("(3,)\n(0,)\n", encoding="utf-8")
            summary = run_local_pipeline(task_dir, output_dir)
            self.assertEqual(summary["input_count"], 2)
            self.assertEqual(summary["success_count"], 2)
            self.assertEqual(summary["failure_count"], 0)
            self.assertGreater(summary["event_count"], 0)
            self.assertEqual(summary["event_count"], summary["probe_count"])
            self.assertTrue((output_dir / "task_1.original.summary.json").exists())


if __name__ == "__main__":
    unittest.main()
