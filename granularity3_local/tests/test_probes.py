import tempfile
import unittest
from pathlib import Path

from granularity3_local.oracle import build_oracle_case
from granularity3_local.probes import build_probe_dataset


class ProbeTests(unittest.TestCase):
    SOURCE = """\
def f(items):
    total = 0
    for item in items:
        total += item
    return total
"""

    def test_model_inputs_and_answers_are_isolated_and_aligned(self):
        with tempfile.TemporaryDirectory() as directory:
            oracle_dir = Path(directory) / "oracle"
            probe_dir = Path(directory) / "probes"
            build_oracle_case(self.SOURCE, "f", "([1, 2],)", "task_test", "original", "input_1", oracle_dir)
            result = build_probe_dataset(oracle_dir, probe_dir)
            self.assertEqual(len(result["model_inputs"]), len(result["answers"]))
            self.assertEqual(
                [item["probe_id"] for item in result["model_inputs"]],
                [item["probe_id"] for item in result["answers"]],
            )
            forbidden = {"next", "delta", "return", "state_after", "return_value", "next_block"}
            for model_input in result["model_inputs"]:
                self.assertTrue(forbidden.isdisjoint(model_input))
            self.assertTrue(result["manifest"]["answer_isolation"])
            self.assertTrue((probe_dir / "answers.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
