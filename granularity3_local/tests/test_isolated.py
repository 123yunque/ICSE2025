import tempfile
import unittest
from pathlib import Path

from granularity3_local.isolated import run_isolated_case


class IsolatedTests(unittest.TestCase):
    def make_code(self, directory, source):
        path = Path(directory) / "code.py"
        path.write_text(source, encoding="utf-8")
        return path

    def test_success_and_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            code = self.make_code(directory, "def f(n):\n    return n + 1\n")
            output = Path(directory) / "result"
            first = run_isolated_case(code, "f", "(1,)", "task_1", "original", "input_1", output)
            second = run_isolated_case(code, "f", "(1,)", "task_1", "original", "input_1", output)
            self.assertEqual(first["status"], "success")
            self.assertEqual(second["status"], "cached")

    def test_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            code = self.make_code(directory, "import time\ndef f():\n    time.sleep(2)\n    return 1\n")
            result = run_isolated_case(
                code, "f", "()", "task_1", "original", "input_1", Path(directory) / "result", timeout_seconds=0.2
            )
            self.assertEqual(result["status"], "timeout")

    def test_event_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            code = self.make_code(directory, "def f(n):\n    total=0\n    while n>0:\n        total+=n\n        n-=1\n    return total\n")
            result = run_isolated_case(
                code,
                "f",
                "(100,)",
                "task_1",
                "original",
                "input_1",
                Path(directory) / "result",
                max_events=10,
            )
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["error_type"], "EventLimitExceeded")

    def test_trace_size_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            code = self.make_code(
                directory,
                "def f(n):\n    values=[]\n    for i in range(n):\n        values.append('x'*1000)\n    return len(values)\n",
            )
            result = run_isolated_case(
                code,
                "f",
                "(20,)",
                "task_1",
                "original",
                "input_1",
                Path(directory) / "result",
                max_trace_bytes=5000,
            )
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["error_type"], "TraceSizeLimitExceeded")


if __name__ == "__main__":
    unittest.main()
