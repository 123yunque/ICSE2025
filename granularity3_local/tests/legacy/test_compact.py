import tempfile
import unittest
from pathlib import Path

from granularity3_local.legacy.compact import build_compact_batches, build_compact_tree, compact_value, expand_value
from granularity3_local.oracle import build_oracle_case
from granularity3_local.legacy.probes import build_probe_dataset


class CompactProbeTests(unittest.TestCase):
    SOURCE = """\
def f(items):
    total = 0
    for item in items:
        if item > 0:
            total += item
    return total
"""

    def test_undefined_round_trip(self):
        original = {"x": {"before": {"$undefined": True}, "after": 0}}
        self.assertEqual(expand_value(compact_value(original)), original)

    def test_batches_are_compact_aligned_and_isolated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            oracle = root / "oracle"
            probes = root / "probes"
            compact = root / "compact"
            build_oracle_case(self.SOURCE, "f", "([1, -2, 3, 0],)", "task_x", "original", "input_1", oracle)
            full = build_probe_dataset(oracle, probes)
            result = build_compact_batches(probes, compact, batch_size=2, max_occurrences=3)
            manifest = result["manifest"]
            self.assertLessEqual(manifest["selected_probe_count"], manifest["original_probe_count"])
            self.assertEqual(len(result["batches"]), len(result["answers"]))
            for batch, answer in zip(result["batches"], result["answers"]):
                self.assertEqual([x["id"] for x in batch["probes"]], [x["id"] for x in answer["answers"]])
                self.assertNotIn("next", str(batch["probes"]))
                self.assertNotIn("delta", str(batch["probes"]))
            self.assertTrue(full["answers"])

    def test_tree_preserves_case_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cases = root / "cases"
            for task in ("task_1", "task_2"):
                oracle = root / task / "oracle"
                probes = cases / task / "original" / "input_1" / "probes"
                build_oracle_case(self.SOURCE, "f", "([1],)", task, "original", "input_1", oracle)
                build_probe_dataset(oracle, probes)
            output = root / "compact"
            summary = build_compact_tree(cases, output, batch_size=8, max_occurrences=3)
            self.assertEqual(summary["case_count"], 2)
            self.assertTrue((output / "task_1" / "original" / "input_1" / "probes" / "manifest.json").exists())
            self.assertTrue((output / "task_2" / "original" / "input_1" / "probes" / "manifest.json").exists())


if __name__ == "__main__":
    unittest.main()
