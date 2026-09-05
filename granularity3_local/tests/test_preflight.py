import json
import tempfile
import unittest
from pathlib import Path

from granularity3_local.preflight import preflight_source, resolve_task_function


class PreflightTests(unittest.TestCase):
    def test_supported_loop(self):
        result = preflight_source("def f(xs):\n    for x in xs:\n        pass\n    return 1\n", "f")
        self.assertTrue(result["supported"])

    def test_jump_and_recursion_are_rejected(self):
        jump = preflight_source("def f(xs):\n    for x in xs:\n        break\n", "f")
        recursive = preflight_source("def f(n):\n    return 1 if n == 0 else f(n-1)\n", "f")
        self.assertEqual(jump["status"], "unsupported_jump")
        self.assertEqual(recursive["status"], "unsupported_recursion")

    def test_metadata_resolves_entry_function(self):
        with tempfile.TemporaryDirectory() as directory:
            task = Path(directory) / "task_1"
            task.mkdir()
            (task / "code.py").write_text("def helper(x): return x\ndef target(x): return helper(x)\n", encoding="utf-8")
            (task / "task_1.json").write_text(json.dumps({"test_list": ["assert target(1) == 1"]}), encoding="utf-8")
            self.assertEqual(resolve_task_function(task), "target")


if __name__ == "__main__":
    unittest.main()
