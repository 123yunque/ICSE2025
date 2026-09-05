import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from granularity3_local.decomposed_api import run_api_experiment, select_requests
from granularity3_local.decomposed_core import (
    CONTROL_FLOW_KIND,
    ORACLE_STATE_KIND,
    STATE_SYSTEM_PROMPT,
    ResponseValidationError,
    make_oracle_response,
    state_sequences_from_events,
    validate_response,
)
from granularity3_local.decomposed_evaluate import (
    build_legacy_compatibility_report,
    build_combined_report,
    evaluate_response_records,
    legacy_state_sequence,
)
from granularity3_local.decomposed_plan import select_stratified_canary
from granularity3_local.decomposed_prepare import (
    prepare_decomposed_dataset,
    prepare_predicted_state_dataset,
)
from granularity3_local.decomposed_statement import execute_statement_state_trace


def write_json(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )


def write_jsonl(path, rows):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


class DecomposedCoreTests(unittest.TestCase):
    def test_state_sequences_use_entry_state_and_every_real_change(self):
        events = [
            {
                "state_before": {"x": 1},
                "state_after": {"x": 1, "y": 2},
                "state_delta": {
                    "y": {"before": {"$undefined": True}, "after": 2}
                },
            },
            {
                "state_before": {"x": 1, "y": 2},
                "state_after": {"x": 1, "y": 2},
                "state_delta": {},
            },
            {
                "state_before": {"x": 1, "y": 2},
                "state_after": {"x": 3, "y": 4},
                "state_delta": {
                    "x": {"before": 1, "after": 3},
                    "y": {"before": 2, "after": 4},
                },
            },
        ]
        self.assertEqual(
            state_sequences_from_events(events),
            {
                "x": [1, 3],
                "y": [{"$u": 1}, 2, 4],
            },
        )

    def test_control_and_state_schemas_are_independent(self):
        control = {
            "kind": CONTROL_FLOW_KIND,
            "request": {"blocks": [["B001", "return 1", [["return", None]]]]},
        }
        self.assertEqual(
            validate_response(control, {"trace": [["B001", 1]]}),
            {"trace": [["B001", 1]]},
        )
        with self.assertRaises(ResponseValidationError):
            validate_response(
                control,
                {"trace": [["B001", 1]], "states": [1]},
            )

        state = {"kind": ORACLE_STATE_KIND, "request": {}}
        self.assertEqual(
            validate_response(state, {"states": [{"$u": 1}, 1]}),
            {"states": [{"$u": 1}, 1]},
        )
        with self.assertRaises(ResponseValidationError):
            validate_response(state, {"states": [1], "trace": [["B001", 1]]})
        self.assertNotIn("trace_source", STATE_SYSTEM_PROMPT)

    def test_statement_trace_keeps_multiple_changes_inside_one_basic_block(self):
        source = (
            "def remove_Occ(s, ch):\n"
            "    s = s.replace(ch, '', 1)\n"
            "    s = s[::-1].replace(ch, '', 1)[::-1]\n"
            "    return s\n"
        )
        traced = execute_statement_state_trace(
            source,
            "remove_Occ",
            ("hello", "l"),
        )
        self.assertEqual(traced["result"], "heo")
        self.assertEqual(
            state_sequences_from_events(traced["events"])["s"],
            ["hello", "helo", "heo"],
        )

    def test_statement_trace_keeps_loop_target_and_each_body_update(self):
        source = (
            "def total(n):\n"
            "    x = 0\n"
            "    for i in range(n):\n"
            "        x += i\n"
            "    return x\n"
        )
        traced = execute_statement_state_trace(source, "total", (3,))
        sequences = state_sequences_from_events(traced["events"])
        self.assertEqual(sequences["i"], [{"$u": 1}, 0, 1, 2])
        self.assertEqual(sequences["x"], [{"$u": 1}, 0, 1, 3])


class DecomposedPipelineTests(unittest.TestCase):
    def _make_local_dataset(self, root):
        local_root = Path(root) / "local"
        case_dir = local_root / "cases" / "task_1" / "input_1"
        case_dir.mkdir(parents=True)
        source = "def f(x):\n    y = x + 1\n    return y\n"
        (case_dir / "code.py").write_text(source, encoding="utf-8")
        blocks = [
            ["B001", "y = x + 1", [["fallthrough", "B002"]]],
            ["B002", "return y", [["return", None]]],
        ]
        write_json(
            case_dir / "model_input.json",
            {"fn": "f(x)", "args": [1], "blocks": blocks},
        )
        write_json(
            case_dir / "local_answer.json",
            {
                "block_trace": ["B001", "B002"],
                "changes": [[0, "y", {"$u": 1}, 2]],
            },
        )
        events = [
            {
                "block_id": "B001",
                "state_before": {"x": 1},
                "state_after": {"x": 1, "y": 2},
                "state_delta": {
                    "y": {"before": {"$undefined": True}, "after": 2}
                },
            },
            {
                "block_id": "B002",
                "state_before": {"x": 1, "y": 2},
                "state_after": {"x": 1, "y": 2},
                "state_delta": {},
            },
        ]
        write_jsonl(case_dir / "oracle" / "events.jsonl", events)
        write_json(
            case_dir / "manifest.json",
            {"function": "f", "normalized_call_args": "(1,)"},
        )
        write_json(case_dir / "oracle" / "case.json", {"result": 2})
        write_jsonl(
            local_root / "case_records.jsonl",
            [{
                "case_key": "task_1/input_1",
                "task_id": "task_1",
                "input_id": "input_1",
                "status": "success",
                "event_count": 2,
                "change_count": 1,
            }],
        )
        return local_root

    def test_prepare_evaluate_and_predicted_state_pipeline(self):
        with tempfile.TemporaryDirectory() as directory:
            local_root = self._make_local_dataset(directory)
            prepared_dir = Path(directory) / "prepared"
            prepared = prepare_decomposed_dataset(
                local_root,
                prepared_dir,
                task_limit=1,
                inputs_per_task=1,
            )
            self.assertEqual(prepared["summary"]["control_flow_request_count"], 1)
            self.assertEqual(prepared["summary"]["oracle_state_request_count"], 1)
            state_request = prepared["state_requests"][0]
            self.assertNotIn("trace_source", state_request["request"])
            self.assertEqual(state_request["control_trace_source"], "oracle")
            self.assertEqual(state_request["request"]["target_variable"], "y")
            self.assertNotIn("changes", state_request["request"])
            self.assertEqual(
                prepared["state_oracles"][0]["answer"]["states"],
                [{"$u": 1}, 2],
            )

            control_evaluation = evaluate_response_records(
                prepared["control_requests"],
                prepared["control_oracles"],
                [make_oracle_response(prepared["control_oracles"][0])],
            )
            state_evaluation = evaluate_response_records(
                prepared["state_requests"],
                prepared["state_oracles"],
                [make_oracle_response(prepared["state_oracles"][0])],
            )
            self.assertEqual(
                control_evaluation["summary"]["expanded_trace_exact_rate_all_requests"],
                1.0,
            )
            self.assertEqual(
                state_evaluation["summary"]["state_exact_rate_all_requests"],
                1.0,
            )
            self.assertEqual(
                state_evaluation["summary"]["state_case_exact_rate_all_cases"],
                1.0,
            )
            self.assertEqual(
                state_evaluation["summary"]["task_macro_state_exact_rate_all_requests"],
                1.0,
            )

            predicted = prepare_predicted_state_dataset(
                prepared["control_requests"],
                [make_oracle_response(prepared["control_oracles"][0])],
                prepared["state_requests"],
                prepared["state_oracles"],
                Path(directory) / "predicted",
                state_request_ids=[
                    state_request["request_id"].replace(
                        "/state/", "/predicted_state/", 1
                    )
                ],
            )
            self.assertEqual(predicted["summary"]["predicted_state_request_count"], 1)
            self.assertNotIn("trace_source", predicted["requests"][0]["request"])
            self.assertEqual(
                predicted["requests"][0]["control_trace_source"],
                "predicted",
            )
            predicted_evaluation = evaluate_response_records(
                predicted["requests"],
                predicted["oracles"],
                [make_oracle_response(predicted["oracles"][0])],
            )
            report = build_combined_report(
                prepared["control_requests"],
                control_evaluation["scores"],
                prepared["state_requests"],
                state_evaluation["scores"],
                predicted["requests"],
                predicted_evaluation["scores"],
            )
            self.assertEqual(report["summary"]["oracle_cf_state_exact_rate"], 1.0)
            self.assertEqual(report["summary"]["predicted_cf_state_exact_rate"], 1.0)
            self.assertEqual(report["summary"]["state_error_propagation_gap"], 0.0)
            self.assertEqual(
                report["summary"]["end_to_end_case_joint_exact_rate"],
                1.0,
            )

    def test_api_runner_can_execute_selected_control_request(self):
        class FakeCompletions:
            last_kwargs = None

            def create(self, **_kwargs):
                self.last_kwargs = _kwargs
                return SimpleNamespace(
                    choices=[SimpleNamespace(
                        message=SimpleNamespace(
                            content='{"trace":[["B001",1]]}'
                        ),
                        finish_reason="stop",
                    )],
                    usage=None,
                    model="fake",
                    _request_id="request-1",
                )

        request = {
            "request_id": "task_1/input_1",
            "case_key": "task_1/input_1",
            "task_id": "task_1",
            "input_id": "input_1",
            "kind": CONTROL_FLOW_KIND,
            "request": {
                "fn": "f()",
                "args": [],
                "blocks": [["B001", "return 1", [["return", None]]]],
            },
        }
        oracle = {
            **{key: value for key, value in request.items() if key != "request"},
            "answer": {"trace": [["B001", 1]]},
        }
        completions = FakeCompletions()
        with tempfile.TemporaryDirectory() as directory:
            summary = run_api_experiment(
                client=SimpleNamespace(
                    chat=SimpleNamespace(completions=completions)
                ),
                requests=[request],
                oracles=[oracle],
                output_dir=directory,
                model="fake",
                api_base_url="https://example.invalid/v1",
                retries=0,
                json_mode=True,
            )
            self.assertEqual(summary["response_count"], 1)
            self.assertEqual(
                completions.last_kwargs["response_format"],
                {"type": "json_object"},
            )
            self.assertEqual(
                summary["evaluation"]["expanded_trace_exact_rate_all_requests"],
                1.0,
            )

    def test_exact_request_selection_preserves_frozen_order(self):
        requests = [
            {
                "request_id": f"task_1/input_{index}",
                "case_key": f"task_1/input_{index}",
                "task_id": "task_1",
                "input_id": f"input_{index}",
            }
            for index in (1, 2, 3)
        ]
        oracles = [
            {"request_id": row["request_id"], "answer": {}}
            for row in requests
        ]
        selected, selected_oracles, _tasks = select_requests(
            requests,
            oracles,
            request_ids=["task_1/input_3", "task_1/input_1"],
        )
        self.assertEqual(
            [row["request_id"] for row in selected],
            ["task_1/input_3", "task_1/input_1"],
        )
        self.assertEqual(
            [row["request_id"] for row in selected_oracles],
            ["task_1/input_3", "task_1/input_1"],
        )

    def test_legacy_report_uses_only_oracle_compatible_variables(self):
        request = {
            "request_id": "task_1/input_1/state/001",
            "case_key": "task_1/input_1",
            "task_id": "task_1",
            "input_id": "input_1",
            "target_variable": "x",
        }
        oracle = {**request, "answer": {"states": [0, 1, 2]}}
        legacy_oracle = [{
            "batch_id": "task_1/input_1",
            "results": [{
                "id": "input_1",
                "block_trace": [["B001", 1], ["B002", 1]],
                "changes": [[0, "x", 0, 1], [1, "x", 1, 2]],
            }],
        }]
        legacy_prediction = [{
            "batch_id": "task_1/input_1",
            "results": [{
                "id": "input_1",
                "block_trace": [["B001", 1], ["B002", 1]],
                "changes": [[0, "x", 0, 1], [1, "x", 1, 2]],
            }],
        }]
        self.assertEqual(
            legacy_state_sequence(legacy_oracle[0]["results"][0]["changes"], "x"),
            {"states": [0, 1, 2], "chain_valid": True},
        )
        report = build_legacy_compatibility_report(
            [request],
            [oracle],
            legacy_oracle,
            legacy_prediction,
            [{"case_key": "task_1/input_1", "expanded_block_exact": True}],
        )
        self.assertEqual(
            report["summary"]["legacy_state_exact_rate_all_compatible_requests"],
            1.0,
        )

    def test_canary_selection_covers_length_bins_with_unique_cases(self):
        candidates = []
        lengths = [2, 3, 6, 11, 26, 101]
        for index, length in enumerate(lengths, start=1):
            candidates.append({
                "request_id": f"task_{index}/input_1/state/001",
                "case_key": f"task_{index}/input_1",
                "task_id": f"task_{index}",
                "state_length": length,
                "state_length_bin": (
                    "2" if length == 2 else
                    "3-5" if length <= 5 else
                    "6-10" if length <= 10 else
                    "11-25" if length <= 25 else
                    "26-100" if length <= 100 else ">100"
                ),
                "state_answer_chars": length,
                "statement_event_count": length,
                "request_chars": length,
            })
        selected = select_stratified_canary(candidates, len(candidates))
        self.assertEqual(len({row["case_key"] for row in selected}), 6)
        self.assertEqual(len({row["state_length_bin"] for row in selected}), 6)


if __name__ == "__main__":
    unittest.main()
