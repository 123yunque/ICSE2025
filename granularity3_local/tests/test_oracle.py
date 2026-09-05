import json
import tempfile
import unittest
from pathlib import Path

from granularity3_local.oracle import build_oracle_case


class OracleTests(unittest.TestCase):
    SOURCE = """\
def count_ones(text, n):
    count = 0
    for i in range(n):
        if text[i] == '1':
            count += 1
    return count
"""

    def test_oracle_files_are_valid_and_repeatable(self):
        with tempfile.TemporaryDirectory() as directory:
            first_dir = Path(directory) / "first"
            second_dir = Path(directory) / "second"
            first = build_oracle_case(
                self.SOURCE, "count_ones", "('011001', 6)", "task_test", "original", "input_1", first_dir
            )
            second = build_oracle_case(
                self.SOURCE, "count_ones", "('011001', 6)", "task_test", "original", "input_1", second_dir
            )
            self.assertTrue(first["case"]["semantics_preserved"])
            self.assertEqual(first["case"]["result"], 3)
            self.assertEqual(first["hashes"], second["hashes"])
            rows = [json.loads(line) for line in (first_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(rows), first["case"]["event_count"])
            self.assertTrue(all("state_before" in row and "state_after" in row for row in rows))
            trace = json.loads((first_dir / "line_trace.json").read_text(encoding="utf-8"))
            self.assertEqual(
                trace["source_lines"],
                [2, 3, 4, 3, 4, 5, 3, 4, 5, 3, 4, 3, 4, 3, 4, 5, 3, 6],
            )
            self.assertEqual(trace["source_lines"], trace["function_relative_lines"])
            self.assertEqual(first["case"]["line_event_count"], len(trace["source_lines"]))


if __name__ == "__main__":
    unittest.main()
