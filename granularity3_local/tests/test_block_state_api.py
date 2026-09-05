import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from granularity3_local.block_state_api import (
    build_generation_config,
    build_messages,
    call_one_batch,
    request_fingerprint,
    run_api_experiment,
    select_task_batches,
    split_one_case_per_request,
    split_selected_batches,
    timeout_for_batch,
)


class BlockStateApiTests(unittest.TestCase):
    def test_request_fingerprint_includes_response_format(self):
        request = {"fn": "f(x)", "blocks": [], "cases": [{"id": "input_1", "args": [1]}]}
        self.assertNotEqual(
            request_fingerprint(request, "single_case"),
            request_fingerprint(request, "flat_runs"),
        )

    def test_request_fingerprint_includes_model_prompt_and_generation_settings(self):
        request = {"fn": "f(x)", "blocks": [], "cases": [{"id": "input_1", "args": [1]}]}
        baseline = build_generation_config(
            "gpt-5.4",
            "https://api.example/v1",
            "flat_runs",
            4096,
            reasoning_effort="none",
            verbosity="low",
            temperature=0,
        )
        changed = dict(baseline)
        changed["reasoning_effort"] = "low"
        self.assertNotEqual(
            request_fingerprint(request, "flat_runs", baseline),
            request_fingerprint(request, "flat_runs", changed),
        )

    def test_selects_requested_tasks_in_requested_order(self):
        models = [
            {"batch_id": "task_2/batch_1", "request": {"cases": []}},
            {"batch_id": "task_1/batch_1", "request": {"cases": []}},
        ]
        oracles = [
            {"batch_id": "task_1/batch_1", "results": []},
            {"batch_id": "task_2/batch_1", "results": []},
        ]
        selected_models, selected_oracles = select_task_batches(
            models, oracles, ["task_1", "task_2"]
        )
        self.assertEqual(
            [row["batch_id"] for row in selected_models],
            ["task_1/batch_1", "task_2/batch_1"],
        )
        self.assertEqual(
            [row["batch_id"] for row in selected_oracles],
            ["task_1/batch_1", "task_2/batch_1"],
        )

    def test_messages_keep_system_prompt_and_request_separate(self):
        request = {"fn": "f(x)", "blocks": [], "cases": [{"id": "input_1", "args": [1]}]}
        messages = build_messages(request)
        self.assertEqual([row["role"] for row in messages], ["system", "user"])
        self.assertIn("complete dynamic sequence", messages[0]["content"])
        self.assertIn('"input_1"', messages[1]["content"])
        self.assertNotIn("block_trace", messages[1]["content"])

    def test_single_case_messages_request_direct_local_answer_shape(self):
        request = {"fn": "f(x)", "blocks": [], "cases": [{"id": "input_1", "args": [1]}]}
        messages = build_messages(request, response_format="single_case")
        self.assertIn("same two fields as the local_answer.json", messages[0]["content"])
        self.assertIn('"block_trace"', messages[0]["content"])
        self.assertNotIn('"results"', messages[0]["content"])

    def test_flat_run_messages_define_only_shallow_trace_and_changes(self):
        request = {"fn": "f(x)", "blocks": [], "cases": [{"id": "input_1", "args": [1]}]}
        messages = build_messages(request, response_format="flat_runs")
        self.assertIn("[path, repeat_count]", messages[0]["content"])
        self.assertIn("Return exactly two fields", messages[0]["content"])
        self.assertNotIn('"results"', messages[0]["content"])

    def test_splits_large_batch_and_preserves_case_oracle_alignment(self):
        model = {
            "batch_id": "task_1/batch_1",
            "request": {
                "fn": "f(x)",
                "blocks": [],
                "cases": [{"id": f"input_{index}", "args": [index]} for index in range(1, 6)],
            },
        }
        oracle = {
            "batch_id": "task_1/batch_1",
            "results": [
                {"id": f"input_{index}", "block_trace": ["B001"], "changes": []}
                for index in range(1, 6)
            ],
        }
        models, oracles = split_selected_batches([model], [oracle], 2)
        self.assertEqual([len(row["request"]["cases"]) for row in models], [2, 2, 1])
        self.assertEqual(
            [[case["id"] for case in row["results"]] for row in oracles],
            [["input_1", "input_2"], ["input_3", "input_4"], ["input_5"]],
        )

    def test_one_case_mode_uses_case_identity_and_keeps_parent_batch(self):
        model = {
            "batch_id": "task_1/batch_1",
            "request": {
                "fn": "f(x)",
                "blocks": [],
                "cases": [
                    {"id": "input_1", "args": [1]},
                    {"id": "input_2", "args": [2]},
                ],
            },
        }
        oracle = {
            "batch_id": "task_1/batch_1",
            "results": [
                {"id": "input_1", "block_trace": ["B001"], "changes": []},
                {"id": "input_2", "block_trace": ["B001"], "changes": []},
            ],
        }
        models, oracles = split_one_case_per_request([model], [oracle])
        self.assertEqual([row["batch_id"] for row in models], ["task_1/input_1", "task_1/input_2"])
        self.assertEqual([len(row["request"]["cases"]) for row in models], [1, 1])
        self.assertEqual([row["parent_batch_id"] for row in models], ["task_1/batch_1"] * 2)
        self.assertEqual([row["batch_id"] for row in oracles], ["task_1/input_1", "task_1/input_2"])

    def test_flat_run_mode_compresses_oracle_before_api_call(self):
        model = {
            "batch_id": "task_1/batch_1",
            "request": {
                "fn": "f(n)",
                "blocks": [
                    ["B001", "i = 0", [["fallthrough", "B002"]]],
                    ["B002", "while i < n", [["loop_body", "B003"], ["loop_exit", "B004"]]],
                    ["B003", "i += 1", [["backedge", "B002"]]],
                    ["B004", "return i", [["return", None]]],
                ],
                "cases": [{"id": "input_1", "args": [2]}],
            },
        }
        oracle = {
            "batch_id": "task_1/batch_1",
            "results": [{
                "id": "input_1",
                "block_trace": ["B001", "B002", "B003", "B002", "B003", "B002", "B004"],
                "changes": [
                    [0, "i", {"$u": 1}, 0],
                    [2, "i", 0, 1],
                    [4, "i", 1, 2],
                ],
            }],
        }
        models, oracles = split_one_case_per_request(
            [model], [oracle], response_format="flat_runs"
        )
        self.assertEqual(models[0]["response_format"], "flat_runs")
        self.assertEqual(oracles[0]["results"][0]["block_trace"], [
            ["B001", 1],
            ["B002>B003", 2],
            ["B002", 1],
            ["B004", 1],
        ])
        self.assertEqual(oracles[0]["results"][0]["changes"], [
            [0, "i", {"$u": 1}, 0],
            [1, "i", 0, 2],
        ])

    def test_resume_skips_only_valid_one_case_responses(self):
        class FakeCompletions:
            def __init__(self):
                self.calls = 0

            def create(self, **kwargs):
                self.calls += 1
                request = json.loads(kwargs["messages"][1]["content"])
                return SimpleNamespace(
                    choices=[SimpleNamespace(
                        message=SimpleNamespace(content=json.dumps({
                            "block_trace": ["B001"],
                            "changes": [],
                        })),
                        finish_reason="stop",
                    )],
                    usage=None,
                    model="fake",
                    _request_id="request-1",
                )

        models = [{
            "batch_id": "task_1/batch_1",
            "request": {
                "fn": "f(x)",
                "blocks": [["B001", "return x", [["return", None]]]],
                "cases": [
                    {"id": "input_1", "args": [1]},
                    {"id": "input_2", "args": [2]},
                ],
            },
        }]
        oracles = [{
            "batch_id": "task_1/batch_1",
            "results": [
                {"id": "input_1", "block_trace": ["B001"], "changes": []},
                {"id": "input_2", "block_trace": ["B001"], "changes": []},
            ],
        }]

        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            first_client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))
            first = run_api_experiment(
                first_client,
                models,
                oracles,
                ["task_1"],
                "fake",
                output_dir,
                one_case_per_request=True,
            )
            self.assertEqual(first["request_mode"], "per_case")
            self.assertEqual(first["response_count"], 2)
            self.assertEqual(first_client.chat.completions.calls, 2)

            second_client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))
            second = run_api_experiment(
                second_client,
                models,
                oracles,
                ["task_1"],
                "fake",
                output_dir,
                one_case_per_request=True,
                resume=True,
            )
            self.assertEqual(second["skipped_batch_count"], 2)
            self.assertEqual(second_client.chat.completions.calls, 0)
            self.assertTrue(second["evaluation"]["complete"])

    def test_invalid_paid_response_is_not_retried_by_default(self):
        class FakeCompletions:
            def __init__(self):
                self.calls = 0

            def create(self, **kwargs):
                self.calls += 1
                return SimpleNamespace(
                    choices=[SimpleNamespace(
                        message=SimpleNamespace(content='{"results":[]}'),
                        finish_reason="stop",
                    )],
                    usage=None,
                    model="fake",
                    _request_id="request-1",
                )

        completions = FakeCompletions()
        client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        batch = {
            "batch_id": "task_1/batch_1",
            "request": {
                "fn": "f(x)",
                "blocks": [["B001", "return x", [["return", None]]]],
                "cases": [{"id": "input_1", "args": [1]}],
            },
        }
        response, attempts = call_one_batch(
            client, batch, "fake", 1, 100, retries=2
        )
        self.assertEqual(completions.calls, 1)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["validation"], "invalid")
        self.assertIsNotNone(response)

    def test_api_call_has_wall_clock_hard_timeout(self):
        class HangingCompletions:
            @staticmethod
            def create(**kwargs):
                time.sleep(0.2)
                raise AssertionError("the daemon worker should outlive the deadline")

        client = SimpleNamespace(
            chat=SimpleNamespace(completions=HangingCompletions())
        )
        batch = {
            "batch_id": "task_1/batch_1",
            "request": {
                "fn": "f(x)",
                "blocks": [["B001", "return x", [["return", None]]]],
                "cases": [{"id": "input_1", "args": [1]}],
            },
        }
        started = time.perf_counter()
        response, attempts = call_one_batch(
            client, batch, "fake", 0.01, 100, retries=0
        )
        self.assertLess(time.perf_counter() - started, 0.15)
        self.assertIsNone(response)
        self.assertEqual(attempts[0]["status"], "api_error")
        self.assertEqual(attempts[0]["error_type"], "TimeoutError")
        self.assertIn("hard timeout", attempts[0]["reason"])

    def test_generation_parameters_and_reasoning_usage_are_logged(self):
        class CapturingCompletions:
            def __init__(self):
                self.kwargs = None

            def create(self, **kwargs):
                self.kwargs = kwargs
                return SimpleNamespace(
                    choices=[SimpleNamespace(
                        message=SimpleNamespace(content=json.dumps({
                            "block_trace": ["B001"],
                            "changes": [],
                        })),
                        finish_reason="stop",
                    )],
                    usage=SimpleNamespace(
                        prompt_tokens=10,
                        completion_tokens=20,
                        total_tokens=30,
                        completion_tokens_details=SimpleNamespace(reasoning_tokens=12),
                        prompt_tokens_details=SimpleNamespace(cached_tokens=4),
                    ),
                    model="gpt-5.4",
                    _request_id="request-1",
                )

        completions = CapturingCompletions()
        batch = {
            "batch_id": "task_1/input_1",
            "response_format": "single_case",
            "request": {
                "fn": "f(x)",
                "blocks": [["B001", "return x", [["return", None]]]],
                "cases": [{"id": "input_1", "args": [1]}],
            },
        }
        _response, attempts = call_one_batch(
            SimpleNamespace(chat=SimpleNamespace(completions=completions)),
            batch,
            "gpt-5.4",
            180,
            4096,
            retries=0,
            reasoning_effort="none",
            verbosity="low",
            temperature=0,
        )
        self.assertEqual(completions.kwargs["reasoning_effort"], "none")
        self.assertEqual(completions.kwargs["verbosity"], "low")
        self.assertEqual(completions.kwargs["max_completion_tokens"], 4096)
        self.assertEqual(attempts[0]["reasoning_tokens"], 12)
        self.assertEqual(attempts[0]["cached_prompt_tokens"], 4)

    def test_nested_loops_receive_complex_timeout(self):
        batch = {
            "request": {
                "blocks": [
                    ["B001", "for x in xs", [["loop_body", "B002"]]],
                    ["B002", "for y in ys", [["loop_body", "B003"]]],
                    ["B003", "pass", [["backedge", "B002"]]],
                ],
            },
        }
        self.assertEqual(timeout_for_batch(batch, 180, 600, 2), (600, 2))

    def test_stage_subset_keeps_full_plan_and_resume_extends_it(self):
        class FakeCompletions:
            def __init__(self):
                self.calls = 0

            def create(self, **_kwargs):
                self.calls += 1
                return SimpleNamespace(
                    choices=[SimpleNamespace(
                        message=SimpleNamespace(content=json.dumps({
                            "block_trace": ["B001"],
                            "changes": [],
                        })),
                        finish_reason="stop",
                    )],
                    usage=None,
                    model="fake",
                    _request_id=f"request-{self.calls}",
                )

        models = [
            {
                "batch_id": "task_1/batch_1",
                "request": {
                    "fn": "f(x)",
                    "blocks": [["B001", "return x", [["return", None]]]],
                    "cases": [
                        {"id": "input_1", "args": [1]},
                        {"id": "input_2", "args": [2]},
                    ],
                },
            },
            {
                "batch_id": "task_2/batch_1",
                "request": {
                    "fn": "g(x)",
                    "blocks": [["B001", "return x", [["return", None]]]],
                    "cases": [{"id": "input_1", "args": [3]}],
                },
            },
        ]
        oracles = [
            {
                "batch_id": "task_1/batch_1",
                "results": [
                    {"id": "input_1", "block_trace": ["B001"], "changes": []},
                    {"id": "input_2", "block_trace": ["B001"], "changes": []},
                ],
            },
            {
                "batch_id": "task_2/batch_1",
                "results": [
                    {"id": "input_1", "block_trace": ["B001"], "changes": []}
                ],
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            first_completions = FakeCompletions()
            first = run_api_experiment(
                SimpleNamespace(chat=SimpleNamespace(completions=first_completions)),
                models,
                oracles,
                ["task_1", "task_2"],
                "fake",
                output_dir,
                one_case_per_request=True,
                stage_case_keys=["task_2/input_1"],
            )
            self.assertEqual(first_completions.calls, 1)
            self.assertEqual(first["selected_case_count"], 3)
            self.assertEqual(first["invocation_selected_case_count"], 1)
            self.assertEqual(first["evaluation"]["expected_case_count"], 1)
            self.assertEqual(first["response_count"], 1)

            second_completions = FakeCompletions()
            second = run_api_experiment(
                SimpleNamespace(chat=SimpleNamespace(completions=second_completions)),
                models,
                oracles,
                ["task_1", "task_2"],
                "fake",
                output_dir,
                one_case_per_request=True,
                resume=True,
                resume_received=True,
            )
            self.assertEqual(second_completions.calls, 2)
            self.assertEqual(second["skipped_batch_count"], 1)
            self.assertEqual(second["invocation_selected_case_count"], 3)
            self.assertEqual(second["evaluation"]["expected_case_count"], 3)
            self.assertEqual(second["response_count"], 3)
            self.assertTrue(second["evaluation"]["complete"])

    def test_resume_rejects_different_generation_configuration(self):
        class FakeCompletions:
            def __init__(self):
                self.calls = 0

            def create(self, **_kwargs):
                self.calls += 1
                return SimpleNamespace(
                    choices=[SimpleNamespace(
                        message=SimpleNamespace(content=json.dumps({
                            "block_trace": ["B001"],
                            "changes": [],
                        })),
                        finish_reason="stop",
                    )],
                    usage=None,
                    model="fake",
                    _request_id="request-1",
                )

        models = [{
            "batch_id": "task_1/batch_1",
            "request": {
                "fn": "f(x)",
                "blocks": [["B001", "return x", [["return", None]]]],
                "cases": [{"id": "input_1", "args": [1]}],
            },
        }]
        oracles = [{
            "batch_id": "task_1/batch_1",
            "results": [{"id": "input_1", "block_trace": ["B001"], "changes": []}],
        }]
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            first = FakeCompletions()
            run_api_experiment(
                SimpleNamespace(chat=SimpleNamespace(completions=first)),
                models,
                oracles,
                ["task_1"],
                "gpt-5.4",
                output_dir,
                one_case_per_request=True,
                reasoning_effort="none",
                verbosity="low",
            )
            second = FakeCompletions()
            with self.assertRaisesRegex(ValueError, "configuration differs"):
                run_api_experiment(
                    SimpleNamespace(chat=SimpleNamespace(completions=second)),
                    models,
                    oracles,
                    ["task_1"],
                    "gpt-5.4",
                    output_dir,
                    one_case_per_request=True,
                    reasoning_effort="low",
                    verbosity="low",
                    resume=True,
                )
            self.assertEqual(second.calls, 0)

    def test_resume_received_preserves_invalid_first_attempt(self):
        class InvalidCompletions:
            def __init__(self):
                self.calls = 0

            def create(self, **kwargs):
                self.calls += 1
                return SimpleNamespace(
                    choices=[SimpleNamespace(
                        message=SimpleNamespace(content='{"results":[]}'),
                        finish_reason="stop",
                    )],
                    usage=None,
                    model="fake",
                    _request_id="request-1",
                )

        models = [{
            "batch_id": "task_1/batch_1",
            "request": {
                "fn": "f(x)",
                "blocks": [["B001", "return x", [["return", None]]]],
                "cases": [{"id": "input_1", "args": [1]}],
            },
        }]
        oracles = [{
            "batch_id": "task_1/batch_1",
            "results": [{"id": "input_1", "block_trace": ["B001"], "changes": []}],
        }]

        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            first_completions = InvalidCompletions()
            run_api_experiment(
                SimpleNamespace(chat=SimpleNamespace(completions=first_completions)),
                models,
                oracles,
                ["task_1"],
                "fake",
                output_dir,
                one_case_per_request=True,
            )
            second_completions = InvalidCompletions()
            summary = run_api_experiment(
                SimpleNamespace(chat=SimpleNamespace(completions=second_completions)),
                models,
                oracles,
                ["task_1"],
                "fake",
                output_dir,
                one_case_per_request=True,
                resume=True,
                resume_received=True,
            )
            self.assertEqual(summary["skipped_batch_count"], 1)
            self.assertEqual(second_completions.calls, 0)
            self.assertEqual(summary["invalid_attempt_count"], 1)
            self.assertFalse(summary["evaluation"]["complete"])


if __name__ == "__main__":
    unittest.main()
