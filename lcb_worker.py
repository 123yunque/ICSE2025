"""Isolated evaluator/Oracle worker; the parent supplies wall-clock termination."""
import importlib.util
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace


class DeadlineAlarm:
    """Windows replacement for POSIX alarms, checked at Python execution events.

    Verdict comparisons remain the upstream checker's. The parent also enforces
    a hard process timeout for non-interruptible native operations.
    """
    SIGALRM = 0

    def __init__(self, exception):
        self.deadline = None
        self.exception = exception
        self.events = 0

    def signal(self, *args):
        return None

    def alarm(self, seconds):
        self.deadline = time.monotonic() + seconds if seconds else None

    def trace(self, frame, event, arg):
        self.events += 1
        if self.events % 256 == 0 and self.deadline is not None and time.monotonic() > self.deadline:
            self.deadline = None
            raise self.exception("per-test deadline exceeded")
        return self.trace


def judge(request):
    import numpy as np
    path = Path(__file__).resolve().parent / "vendor" / "testing_util.py"
    spec = importlib.util.spec_from_file_location("lcb_official_testing", path)
    checker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checker)
    alarm = DeadlineAlarm(checker.TimeoutException)
    checker.signal = alarm
    sample = json.loads(Path(request["sample_path"]).read_text(encoding="utf-8"))
    code = Path(request["submission_path"]).read_text(encoding="utf-8")
    previous = sys.gettrace()
    sys.settrace(alarm.trace)
    try:
        verdicts, metadata = checker.run_test(sample, code, timeout=request["per_test_timeout"])
    finally:
        sys.settrace(previous)
    passed = len(verdicts) == request["expected_test_count"] and all(
        isinstance(v, (bool, np.bool_)) and bool(v) for v in verdicts)
    return {"status": "passed" if passed else "test_failed", "tested_count": len(verdicts),
            "expected_test_count": request["expected_test_count"],
            "verdicts": [bool(v) if isinstance(v, (bool, np.bool_)) else int(v) for v in verdicts],
            "metadata": metadata, "checker": "official testing_util.py with Windows deadline adapter"}


def oracle(request):
    from granularity3_local.block_state_local import prepare_local_case
    from granularity3_local.decomposed_prepare import prepare_decomposed_dataset
    from granularity3_local.oracle import write_jsonl
    root = Path(request["local_root"])
    task_id, input_id = request["task_id"], request["input_id"]
    source_path = Path(request["code_path"])
    source = source_path.read_text(encoding="utf-8")
    case_dir = root / "cases" / task_id / input_id
    result = prepare_local_case(source, "solve", request["input_text"], task_id, input_id,
                                case_dir, source_path=source_path, max_events=500,
                                max_trace_bytes=4 * 1024 * 1024)
    manifest = result["manifest"]
    record = {"task_id": task_id, "input_id": input_id, "case_key": f"{task_id}/{input_id}",
              "status": "success", "event_count": manifest["event_count"],
              "change_count": manifest["change_count"]}
    write_jsonl(root / "case_records.jsonl", [record])
    prepared = root / "prepared"
    summary = prepare_decomposed_dataset(root, prepared, max_events=500, max_statement_events=2000,
                     max_state_items=500, max_state_answer_chars=16000)["summary"]
    return {**record, "prepared_dir": str(prepared.resolve()), "case_dir": str(case_dir.resolve()),
            "prepare_summary": summary}


if __name__ == "__main__":
    request = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    # Open the known response handle before the official reliability guard disables filesystem mutations.
    with open(sys.argv[2], "w", encoding="utf-8") as output:
        try:
            result = judge(request) if request["mode"] == "judge" else oracle(request)
        except BaseException as exc:
            result = {"status": "failed", "error_type": type(exc).__name__, "reason": str(exc)[:1000]}
        output.write(json.dumps(result, ensure_ascii=False, default=str) + "\n")
