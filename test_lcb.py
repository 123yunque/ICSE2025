import base64
import hashlib
import json
import pickle
import tempfile
import unittest
from types import SimpleNamespace
import zlib
from pathlib import Path

from lcb_experiment import check_core, choose_tests, decode_tests, test_args, submission, worker_process, save


class AdapterTests(unittest.TestCase):
    def test_opaque_states_and_helper_only_boundary(self):
        from lcb_experiment import opaque_state, helper_only_target
        self.assertTrue(opaque_state([{'$u': 1}, {'$o': ['function', 'address']}]))
        self.assertFalse(opaque_state([{'$u': 1}, {'$d': [['key', 2]]}]))
        self.assertTrue(helper_only_target('def solve(n):\n    def helper(x):\n        return x+1\n    return helper(n)'))
        self.assertFalse(helper_only_target('def solve(n):\n    return len(set(n))'))
        self.assertFalse(helper_only_target('def solve(n):\n    def helper(x):\n        return x+1\n    for i in n:\n        helper(i)\n    return 0'))

    def test_pilot_resume_keeps_first_invalid_response(self):
        from granularity3_local.decomposed_api import run_api_experiment
        from granularity3_local.decomposed_core import read_jsonl
        calls = []
        def create(**kwargs):
            calls.append(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"wrong":1}'),
                                                            finish_reason='stop')])
        requests = [{'request_id': 'task_0/input_' + str(i), 'case_key': 'task_0/input_' + str(i),
                     'task_id': 'task_0', 'input_id': 'input_' + str(i), 'kind': 'control_flow',
                     'request': {'fn': 'solve()', 'args': [], 'blocks': [['B001', 'return 1', []]]}} for i in range(3)]
        oracles = [{**r, 'answer': {'trace': [['B001', 1]]}} for r in requests]
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with tempfile.TemporaryDirectory() as temp:
            kwargs = dict(client=client, requests=requests, oracles=oracles, output_dir=temp,
                          model='fake', api_base_url='https://example.invalid', retries=0,
                          retry_invalid=False, concurrency=1, resume_received=True)
            first = run_api_experiment(**kwargs, dispatch_request_ids=[requests[1]['request_id']])
            self.assertEqual(first['resumed_response_count'], 0)
            self.assertEqual(len(calls), 1)
            second = run_api_experiment(**kwargs, resume=True)
            self.assertEqual(second['resumed_response_count'], 1)
            self.assertEqual(len(calls), 3)
            self.assertEqual(len(read_jsonl(Path(temp) / 'model_responses.jsonl')), 3)

    def test_official_encoded_private_tests(self):
        rows = [{"input": "1", "output": "2", "testtype": "functional"}]
        encoded = base64.b64encode(zlib.compress(pickle.dumps(json.dumps(rows)))).decode()
        self.assertEqual(decode_tests(encoded), rows)

    def test_pickle_rejects_classes(self):
        encoded = base64.b64encode(zlib.compress(pickle.dumps(Path("not_loaded")))).decode()
        with self.assertRaises(ValueError):
            decode_tests(encoded)

    def test_positional_arguments_and_single_list(self):
        self.assertEqual(test_args({"testtype": "functional", "input": "[1,2]\n3"}), ([1, 2], 3))
        self.assertEqual(test_args({"testtype": "functional", "input": "[1,2]"}), ([1, 2],))
        self.assertEqual(test_args({"testtype": "stdin", "input": "1\n2\n"}), ("1\n2\n",))

    def test_selection_deduplicates_and_never_changes_input(self):
        rows = [{"input": str(i), "output": str(i + 1), "testtype": "functional"} for i in range(20)]
        a = choose_tests(rows[:5], rows[3:], "p/1")
        self.assertEqual(a, choose_tests(rows[:5], rows[3:], "p/1"))
        self.assertEqual(len(a), 10)
        self.assertEqual(len({r["input"] for r in a}), 10)
        self.assertEqual([r["input"] for r in a[:3]], ["0", "1", "2"])

    def test_io_wrapper_and_core_contract(self):
        check_core("def solve(s):\n    return str(int(s) + 1)", "stdin")
        with self.assertRaises(ValueError):
            check_core("import os\ndef solve(s): return os.listdir(s)", "stdin")
        with self.assertRaises(ValueError):
            check_core("def solve(s): return open(s).read()", "stdin")

    def test_official_function_and_stdio_checks(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for kind in ("functional", "stdin"):
                fn = "addOne" if kind == "functional" else None
                code = "def solve(x):\n    return x + 1" if fn else "def solve(x):\n    return str(int(x) + 1) + '\\n'"
                (root / "code.py").write_text(submission(code, fn), encoding="utf-8")
                save(root / "sample.json", {"input_output": json.dumps({"inputs": ["1", "2"],
                    "outputs": ["2", "3"], "fn_name": fn})})
                verdict = worker_process({"mode": "judge", "sample_path": str(root / "sample.json"),
                    "submission_path": str(root / "code.py"), "expected_test_count": 2,
                    "per_test_timeout": 2}, root / kind, 20)
                self.assertEqual(verdict["status"], "passed", verdict)

    def test_oracle_captures_multiple_updates_within_block(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = "def solve(n):\n    x = 1\n    x = 2\n    for i in range(n):\n        x += i\n    return x\n"
            (root / "code.py").write_text(source, encoding="utf-8")
            result = worker_process({"mode": "oracle", "task_id": "task_0", "input_id": "input_0",
                "code_path": str(root / "code.py"), "input_text": "(3,)",
                "local_root": str(root / "local")}, root / "worker", 20)
            self.assertEqual(result["status"], "success", result)
            rows = [json.loads(line) for line in (Path(result["prepared_dir"]) / "oracle_state/oracles.jsonl").read_text().splitlines()]
            states = next(row["answer"]["states"] for row in rows if row["target_variable"] == "x")
            self.assertEqual(states, [{"$u": 1}, 1, 2, 3, 5])


if __name__ == "__main__":
    unittest.main()
