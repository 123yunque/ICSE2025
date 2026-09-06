"""Fresh, resumable release_v6 experiment; no historical model/oracle reuse."""
from __future__ import annotations

import argparse
import ast
import base64
import copy
import hashlib
import importlib.util
import io
import json
import os
import pickle
import random
import re
import subprocess
import sys
import threading
import time
import types
import zlib
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from granularity3_local.block_state_api import DEFAULT_API_BASE_URL, create_http_compatible_client
from granularity3_local.decomposed_core import read_jsonl, compact_json
from granularity3_local.oracle import write_json, write_jsonl
from granularity3_local.preflight import preflight_source

ROOT = Path(__file__).resolve().parent
ALLOWED_IMPORTS = {"typing", "collections", "heapq", "bisect", "math", "itertools", "functools",
                   "operator", "statistics", "string", "re", "decimal", "fractions", "array", "copy",
                   "random", "sys", "dataclasses", "enum", "json"}
DENIED_NAMES = {"eval", "exec", "open", "compile", "__import__", "globals", "locals", "vars",
                "getattr", "setattr", "delattr", "breakpoint", "input", "print", "exit", "quit"}
GEN_PROMPT = """Solve the programming problem correctly and efficiently in Python 3.9.
Return one JSON object with exactly one field: code, containing a complete Python source string.
The source must define a top-level function named solve containing the main algorithm.
Include every required import explicitly. Do not execute the function at module scope.
Do not return a Solution class or a main block; an external adapter supplies that interface.
For functional problems, solve accepts the original method's positional arguments (without self)
and returns the required Python result.
For stdin problems, solve accepts exactly one string raw_input holding the entire official stdin
(including all test groups), parses it, and returns the entire output as a string.
Do not read stdin or print; use the supplied arguments and return value.
Keep the main algorithm in solve, rather than a trivial wrapper around another function.
Helper functions for genuine subroutines are allowed. Normal loops, branches, recursion, and
break/continue may be used when appropriate; do not simplify the algorithm for a tracer.
Use only standard algorithm libraries; no filesystem, network, reflection, dynamic code execution,
or process operations. If sys is needed, use only sys.setrecursionlimit or sys.maxsize.
Return JSON only, without explanations or Markdown."""


def sha(value):
    return hashlib.sha256((value if isinstance(value, bytes) else compact_json(value).encode("utf-8"))).hexdigest()


def save(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    write_json(path, value)


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def file_sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def audit_errata(row, tests):
    """Confirm applicable defects in this frozen release, not historical counts."""
    entries = set(re.findall(r'\d+\. ([a-z0-9_-]+) -', (ROOT / 'vendor/ERRATA.md').read_text()))
    title = re.sub(r'[^a-z0-9]+', '-', row['question_title'].lower()).strip('-')
    match = next((key for key in (row['question_id'], title) if key in entries), None)
    if not match:
        return {'status': 'no_listed_issue'}
    if match == 'most-frequent-ids':
        for index, test in enumerate(tests):
            nums, freq = test_args(test)
            counts = Counter()
            for step, (number, delta) in enumerate(zip(nums, freq)):
                counts[number] += delta
                if counts[number] < 0:
                    return {'status': 'confirmed_data_issue', 'entry': match,
                            'reason': 'official input violates nonnegative ID-frequency constraint',
                            'test_index': index, 'step': step, 'id': number, 'count': counts[number]}
        return {'status': 'listed_issue_not_observed', 'entry': match,
                'check': 'all per-ID cumulative counts remain nonnegative'}
    return {'status': 'requires_judge_audit', 'entry': match}


class DataUnpickler(pickle.Unpickler):
    def find_class(self, module, name):
        raise ValueError("dataset pickle must contain data only")


def decode_tests(raw):
    if isinstance(raw, list):
        return raw
    try:
        result = json.loads(raw)
    except (ValueError, TypeError):
        content = zlib.decompress(base64.b64decode(raw))
        result = DataUnpickler(io.BytesIO(content)).load()
        if isinstance(result, str):
            result = json.loads(result)
    if not isinstance(result, list):
        raise ValueError("tests must be a list")
    for row in result:
        if not all(key in row for key in ("input", "output", "testtype")):
            raise ValueError("invalid test record")
    return result


def test_args(test):
    if test["testtype"] == "stdin":
        return (test["input"],)
    # Exactly the newline-separated JSON arguments used by the official checker.
    return tuple(json.loads(line) for line in test["input"].split("\n"))


def choose_tests(public, private, key, limit=10):
    seen, pools = {}, {"public": [], "private": []}
    for label, rows in (("public", public), ("private", private)):
        for index, row in enumerate(rows):
            identity = (row["testtype"], row["input"])
            if identity in seen:
                if seen[identity] != row["output"]:
                    raise ValueError("duplicate input has conflicting outputs")
                continue
            seen[identity] = row["output"]
            pools[label].append({**row, "source": label, "source_index": index})
    rng = random.Random(int(sha({"seed": 20260905, "key": key}), 16))
    rng.shuffle(pools["private"])
    public_n = min(3, limit, len(pools["public"]))
    private_n = min(max(0, limit - public_n), len(pools["private"]))
    chosen = pools["public"][:public_n] + pools["private"][:private_n]
    rest = pools["public"][public_n:] + pools["private"][private_n:]
    return chosen + rest[:max(0, limit - len(chosen))]


def check_core(code, kind):
    tree = ast.parse(code)
    functions = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "solve"]
    if len(functions) != 1:
        raise ValueError("exactly one top-level solve is required")
    for node in tree.body:
        if not isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.Assign, ast.AnnAssign, ast.Expr)):
            raise ValueError("unsupported module-level declaration")
        if isinstance(node, ast.Expr) and not isinstance(node.value, ast.Constant):
            if not (isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Attribute)
                    and isinstance(node.value.func.value, ast.Name) and node.value.func.value.id == "sys"
                    and node.value.func.attr == "setrecursionlimit"):
                raise ValueError("module-level execution is not allowed")
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            modules = [alias.name for alias in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            if any(module.split(".")[0] not in ALLOWED_IMPORTS for module in modules):
                raise ValueError("non-algorithm import")
        if isinstance(node, ast.Name) and node.id in DENIED_NAMES:
            raise ValueError(f"disallowed operation {node.id}")
        if isinstance(node, ast.Attribute):
            if node.attr.startswith("__"):
                raise ValueError("dunder access is not allowed")
            if isinstance(node.value, ast.Name) and node.value.id == "sys" and node.attr not in {"setrecursionlimit", "maxsize"}:
                raise ValueError("core must not access sys IO/process state")
    if kind == "stdin" and (len(functions[0].args.args) != 1 or functions[0].args.kwonlyargs):
        raise ValueError("stdin solve must accept one positional string")
    return tree


def submission(code, fn_name):
    if fn_name:
        if not str(fn_name).isidentifier():
            raise ValueError("invalid function name")
        return code + f"\n\nclass Solution:\n    def {fn_name}(self, *args):\n        return solve(*args)\n"
    return code + '\n\nif __name__ == "__main__":\n    import sys\n    sys.stdout.write(solve(sys.stdin.read()))\n'


def child_env():
    return {k: v for k, v in os.environ.items()
            if not any(token in k.upper() for token in ("API_KEY", "TOKEN", "SECRET", "PASSWORD"))}


def worker_process(request, directory, timeout):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    request_path, response_path = directory / "worker_request.json", directory / "worker_response.json"
    save(request_path, request)
    if response_path.exists():
        response_path.unlink()
    started = time.monotonic()
    try:
        result = subprocess.run([sys.executable, "-B", str(ROOT / "lcb_worker.py"),
                                 str(request_path.resolve()), str(response_path.resolve())],
                                cwd=str(ROOT), env=child_env(), timeout=timeout,
                                capture_output=True, encoding="utf-8", errors="replace")
        if response_path.exists():
            output = load(response_path)
        else:
            output = {"status": "worker_error", "exit_code": result.returncode,
                      "detail": result.stderr[-1500:]}
    except subprocess.TimeoutExpired:
        output = {"status": "worker_timeout", "timeout_seconds": timeout}
    output["wall_seconds"] = time.monotonic() - started
    return output


def generate_one(row, run, model, candidate_no, client):
    dest = run / "solutions" / row["task_id"] / f"candidate_{candidate_no}"
    dest.mkdir(parents=True, exist_ok=True)
    received_path = dest / "received.json"
    config = {"model": model, "reasoning_effort": "high", "verbosity": "low",
              "max_completion_tokens": 16384, "response_format": {"type": "json_object"}}
    messages = [{"role": "system", "content": GEN_PROMPT}, {"role": "user", "content": compact_json({
        "problem": row["question_content"], "starter_code": row.get("starter_code", ""),
        "interface": row["interface"], "original_function_name": row.get("fn_name")})}]
    request = {"run_id": run.name, "task_id": row["task_id"], "candidate": candidate_no,
               "config": config, "messages": messages}
    request["fingerprint"] = sha(request)
    request_path = dest / "request.json"
    if request_path.exists() and load(request_path)["fingerprint"] != request["fingerprint"]:
        raise ValueError("candidate configuration changed")
    save(request_path, request)
    if received_path.exists():
        return load(received_path)
    started = time.monotonic()
    try:
        response = client.chat.completions.create(**config, messages=messages, timeout=240)
        usage = getattr(response, "usage", None)
        result = {"status": "received", "timestamp": datetime.now(timezone.utc).isoformat(),
                  "raw_response": response.choices[0].message.content or "",
                  "finish_reason": response.choices[0].finish_reason,
                  "response_model": getattr(response, "model", None),
                  "prompt_tokens": getattr(usage, "prompt_tokens", 0),
                  "completion_tokens": getattr(usage, "completion_tokens", 0),
                  "request_fingerprint": request["fingerprint"], "elapsed_seconds": time.monotonic() - started}
        save(received_path, result)
        return result
    except Exception as exc:
        result = {"status": "api_error", "error": str(exc)[:1000], "type": type(exc).__name__,
                  "elapsed_seconds": time.monotonic() - started}
        save(dest / f"transport_error_{time.time_ns()}.json", result)
        return result


def build_problem(row, run, client, model, inputs_per_task):
    task_dir = run / "solutions" / row["task_id"]
    result_path = task_dir / "result.json"
    if result_path.exists():
        return load(result_path)
    task_dir.mkdir(parents=True, exist_ok=True)
    result = {"task_id": row["task_id"], "problem_key": row["problem_key"]}
    try:
        public, private = decode_tests(row["public_test_cases"]), decode_tests(row["private_test_cases"])
        tests = public + private
        audit = audit_errata(row, tests)
        save(task_dir / 'errata_audit.json', audit)
        if audit['status'] in {'confirmed_data_issue', 'requires_judge_audit'}:
            result.update(status='data_issue', errata=audit)
            save(result_path, result)
            return result
        kinds = {t["testtype"] for t in tests}
        if len(kinds) != 1 or not tests:
            raise ValueError("missing or mixed test interfaces")
        kind = next(iter(kinds))
        metadata = json.loads(row["metadata"]) if isinstance(row["metadata"], str) else row["metadata"]
        fn_name = metadata.get("func_name") if kind == "functional" else None
        if kind == "functional" and not fn_name:
            raise ValueError("functional problem has no func_name")
        row = {**row, "interface": kind, "fn_name": fn_name}
        chosen = choose_tests(public, private, row["problem_key"], inputs_per_task)
        save(task_dir / "selected_tests.json", chosen)
        # Hidden tests are local only, never included in generation messages.
        sample = {"input_output": json.dumps({"inputs": [t["input"] for t in tests],
                 "outputs": [t["output"] for t in tests], "fn_name": fn_name})}
        save(task_dir / "test_sample.json", sample)
        result.update(interface=kind, total_tests=len(tests), public_tests=len(public), private_tests=len(private),
                      selected_test_count=len(chosen))
        attempts = []
        for candidate in range(1, 4):
            dest = task_dir / f"candidate_{candidate}"
            reply = generate_one(row, run, model, candidate, client)
            if reply["status"] == "api_error":
                # Infrastructure failures do not consume a candidate or fabricate a received answer.
                result.update(status="api_incomplete", attempts=attempts)
                return result
            try:
                payload = json.loads(reply["raw_response"])
                code = payload["code"]
                if reply.get("finish_reason") != "stop" or not isinstance(code, str):
                    raise ValueError("truncated or invalid code answer")
                check_core(code, kind)
                core_path = dest / "code.py"
                core_path.write_text(code + "\n", encoding="utf-8")
                (dest / "submission.py").write_text(submission(code, fn_name), encoding="utf-8")
                judge_path = dest / "judge.json"
                verdict = load(judge_path) if judge_path.exists() else worker_process({
                    "mode": "judge", "sample_path": str((task_dir / "test_sample.json").resolve()),
                    "submission_path": str((dest / "submission.py").resolve()),
                    "expected_test_count": len(tests), "per_test_timeout": 6}, dest / "judge_worker", 180)
                save(judge_path, verdict)
                attempts.append({"candidate": candidate, "verdict": verdict})
                if verdict.get("status") != "passed":
                    continue
                (task_dir / "code.py").write_text(code + "\n", encoding="utf-8")
                result.update(status="verified", selected_candidate=candidate, code_sha256=sha((code + "\n").encode()),
                              preflight=preflight_source(code, "solve"), attempts=attempts)
                save(result_path, result)
                return result
            except Exception as exc:
                attempts.append({"candidate": candidate, "status": "invalid_candidate", "reason": str(exc)[:500]})
        result.update(status="no_verified_solution", attempts=attempts)
    except Exception as exc:
        result.update(status="data_error", reason=str(exc)[:1000])
    save(result_path, result)
    return result


def build(run, data, model="gpt-5.4", concurrency=3, inputs_per_task=10):
    run = Path(run)
    with (Path(data) / 'selected.jsonl').open(encoding='utf-8') as handle:
        rows = [json.loads(line) for line in handle if line.strip()]
    manifest = load(Path(data) / "manifest.json")
    if file_sha(Path(data) / "selected.jsonl") != manifest["selected_sha256"]:
        raise ValueError("cohort checksum mismatch")
    base_url = os.getenv("YUNWU_API_BASE_URL", DEFAULT_API_BASE_URL)
    config = {"run_id": run.name, "started_from": "ea510130f7c09ab7c0e0d0d8a3398bf2c65d8ddd",
              "cohort_sha256": manifest["selected_sha256"], "model": model, "inputs_per_task": inputs_per_task,
              "concurrency": concurrency, "api_base_url": base_url, "generation_prompt_sha256": sha(GEN_PROMPT.encode()),
              "cohort_count": len(rows), "fresh_generation": True,
              "python_version": sys.version, "checker_source": load(ROOT / 'vendor/SOURCE.json'),
              "limits": {"block_events": 500, "statement_events": 2000, "state_items": 500,
                         "state_answer_chars": 16000, "trace_bytes": 4194304,
                         "judge_per_test_seconds": 6, "judge_process_seconds": 180,
                         "oracle_process_seconds": 90}}
    if (run / "config.json").exists() and load(run / "config.json") != config:
        raise ValueError("run configuration changed")
    save(run / "config.json", config)
    save(run / "cohort.json", manifest)
    client = create_http_compatible_client(os.environ["YUNWU_API_KEY"], base_url)
    results = []
    with ThreadPoolExecutor(concurrency) as pool:
        futures = {pool.submit(build_problem, row, run, client, model, inputs_per_task): row for row in rows}
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            print(f"build {len(results)}/{len(rows)} {result['task_id']} {result['status']}", flush=True)
            write_jsonl(run / "build_results.jsonl", sorted(results, key=lambda r: r["task_id"]))
    save(run / "build_summary.json", {"counts": dict(Counter(r["status"] for r in results)),
         "total": len(rows), "supported_verified": sum(r.get("preflight", {}).get("supported", False) for r in results)})


def oracle_case(run, result, index, test):
    task_id, input_id = result["task_id"], f"input_{index:03d}"
    dest = run / "local" / "cases" / task_id / input_id
    outcome = dest / "fresh_result.json"
    if outcome.exists():
        return load(outcome)
    try:
        args = test_args(test)
        record = worker_process({"mode": "oracle", "task_id": task_id, "input_id": input_id,
            "code_path": str((run / "solutions" / task_id / "code.py").resolve()),
            "input_text": repr(args), "case_dir": str(dest.resolve()),
            "local_root": str((run / "case_work" / task_id / input_id).resolve())},
            run / "oracle_workers" / task_id / input_id, 90)
    except Exception as exc:
        record = {"status": "failed", "reason": str(exc)[:1000]}
    record.update(task_id=task_id, input_id=input_id, case_key=f"{task_id}/{input_id}",
                  test_source=test["source"], test_source_index=test["source_index"])
    save(outcome, record)
    return record


def opaque_state(value):
    if isinstance(value, dict):
        return '$o' in value or any(opaque_state(v) for v in value.values())
    return isinstance(value, list) and any(opaque_state(v) for v in value)


def helper_only_target(code):
    tree = ast.parse(code)
    function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'solve')
    helper_names = {n.name for n in function.body if isinstance(n, ast.FunctionDef)}
    outer = [n for n in function.body if not isinstance(n, ast.FunctionDef)]
    has_control = any(isinstance(n, (ast.For, ast.While, ast.If, ast.Try, ast.With))
                      for statement in outer for n in ast.walk(statement))
    helper_calls = any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in helper_names
                       for statement in outer for n in ast.walk(statement))
    return bool(helper_names and helper_calls and not has_control)


def prepare(run, concurrency=4):
    run = Path(run)
    results = read_jsonl(run / "build_results.jsonl")
    jobs, exclusions = [], []
    save(run / 'preparation_policy.json', {'version': 'lcb-adapter-eligibility-v1',
         'opaque_states': 'exclude variables with $o process-specific object representation',
         'helper_only_target': 'exclude target calling nested helpers without target-level control flow',
         'timing': 'frozen before any execution-inference model response; solution candidate choice unchanged'})
    for result in results:
        if result["status"] != "verified" or not result["preflight"]["supported"]:
            exclusions.append({"task_id": result["task_id"], "scope": "problem",
                "reason": result.get("preflight", {}).get("status", result["status"])})
            continue
        if helper_only_target((run / 'solutions' / result['task_id'] / 'code.py').read_text(encoding='utf-8')):
            exclusions.append({'task_id': result['task_id'], 'scope': 'problem', 'reason': 'helper_only_target'})
            continue
        for index, test in enumerate(load(run / "solutions" / result["task_id"] / "selected_tests.json")):
            jobs.append((result, index, test))
    records = []
    with ThreadPoolExecutor(concurrency) as pool:
        futures = [pool.submit(oracle_case, run, result, index, test) for result, index, test in jobs]
        for future in as_completed(futures):
            record = future.result()
            records.append(record)
            if len(records) % 20 == 0 or record["status"] != "success":
                print(f"oracle {len(records)}/{len(jobs)} {record['case_key']} {record['status']}", flush=True)
    records.sort(key=lambda r: r["case_key"])
    (run / "prepared").mkdir(exist_ok=True)
    controls, control_oracles, states, state_oracles = [], [], [], []
    manifests = []
    for record in records:
        if record["status"] != "success":
            exclusions.append({**record, "scope": "case"})
            continue
        part = Path(record["prepared_dir"])
        controls += read_jsonl(part / "control_flow" / "requests.jsonl")
        control_oracles += read_jsonl(part / "control_flow" / "oracles.jsonl")
        states += read_jsonl(part / "oracle_state" / "requests.jsonl")
        state_oracles += read_jsonl(part / "oracle_state" / "oracles.jsonl")
        manifests += read_jsonl(part / "case_manifest.jsonl")
        exclusions += read_jsonl(part / "excluded.jsonl")
    rejected_ids = {r['request_id'] for r in state_oracles if opaque_state(r['answer']['states'])}
    for row in states:
        if row['request_id'] in rejected_ids:
            exclusions.append({'task_id': row['task_id'], 'case_key': row['case_key'],
                'target_variable': row['target_variable'], 'scope': 'variable',
                'reason': 'opaque_object_state', 'request_id': row['request_id']})
    states = [r for r in states if r['request_id'] not in rejected_ids]
    state_oracles = [r for r in state_oracles if r['request_id'] not in rejected_ids]
    kept_by_case = {}
    for row in states:
        kept_by_case.setdefault(row['case_key'], []).append(row['target_variable'])
    for row in manifests:
        row['pre_adapter_variable_count'] = row['tracked_variable_count']
        row['tracked_variables'] = kept_by_case.get(row['case_key'], [])
        row['tracked_variable_count'] = len(row['tracked_variables'])
        row['opaque_variables_excluded'] = row['pre_adapter_variable_count'] - row['tracked_variable_count']
        if row['opaque_variables_excluded'] and not row['tracked_variable_count']:
            row['state_status'] = 'excluded_opaque_variables'
    from granularity3_local.decomposed_core import make_oracle_response
    from granularity3_local.decomposed_evaluate import evaluate_response_records
    for kind, requests, oracles in [("control_flow", controls, control_oracles), ("oracle_state", states, state_oracles)]:
        path = run / "prepared" / kind
        path.mkdir(exist_ok=True)
        write_jsonl(path / "requests.jsonl", requests)
        write_jsonl(path / "oracles.jsonl", oracles)
        responses = [make_oracle_response(row) for row in oracles]
        write_jsonl(path / "oracle_responses.jsonl", responses)
        if requests:
            evaluation = evaluate_response_records(requests, oracles, responses, path / "selfcheck")
            metric = 'expanded_trace_exact_rate_all_requests' if kind == 'control_flow' else 'state_exact_rate_all_requests'
            if not evaluation['summary']['fully_valid'] or evaluation['summary'][metric] != 1.0:
                raise ValueError("oracle selfcheck failed")
    write_jsonl(run / "prepared" / "case_manifest.jsonl", manifests)
    write_jsonl(run / "prepared" / "excluded.jsonl", exclusions)
    write_jsonl(run / "local" / "case_records.jsonl", records)
    summary = {"problem_count": len(results), "selected_case_count": len(jobs), "control_request_count": len(controls),
               "oracle_state_request_count": len(states), "state_case_count": len({s["case_key"] for s in states}),
               "eligible_problem_count": len({c["task_id"] for c in controls}),
               "case_status_counts": dict(Counter(r["status"] for r in records)),
               "requests_sha256": {"control": sha(controls), "state": sha(states)}}
    save(run / "prepared" / "summary.json", summary)
    print(json.dumps(summary), flush=True)


def pilot_ids(run, kind, requests):
    path = run / 'pilot' / (kind + '_ids.json')
    if path.exists():
        return load(path)
    interfaces = {r['task_id']: r.get('interface') for r in read_jsonl(run / 'build_results.jsonl')}
    by_case = {}
    for row in requests:
        by_case.setdefault(row['case_key'], []).append(row)
    rng = random.Random(20260905)
    groups = {}
    for case, rows in sorted(by_case.items()):
        groups.setdefault(interfaces[rows[0]['task_id']], []).append(case)
    chosen = []
    for group in groups.values():
        rng.shuffle(group)
    while len(chosen) < min(40, len(by_case)):
        for name in sorted(groups):
            if groups[name] and len(chosen) < 40:
                case = groups[name].pop()
                chosen.append(rng.choice(by_case[case])['request_id'])
    save(path, chosen)
    return chosen


def infer(run, kind, concurrency=3, pilot=False):
    from granularity3_local.decomposed_api import run_api_experiment
    run = Path(run)
    config = load(run / "config.json")
    path = run / "prepared" / kind
    requests, oracles = read_jsonl(path / "requests.jsonl"), read_jsonl(path / "oracles.jsonl")
    if not requests:
        return
    output = run / kind
    dispatch = pilot_ids(run, kind, requests) if pilot else None
    client = create_http_compatible_client(os.environ["YUNWU_API_KEY"], config["api_base_url"])
    summary = run_api_experiment(client, requests, oracles, output, config["model"], config["api_base_url"],
        timeout=180, max_completion_tokens=16384, retries=0, retry_invalid=False,
        reasoning_effort="low", verbosity="low", concurrency=concurrency, json_mode=True,
        resume=(output / "run_config.json").exists(), resume_received=True, progress_every=50,
        dispatch_request_ids=dispatch)
    if pilot:
        from granularity3_local.decomposed_evaluate import evaluate_response_records
        ids = set(dispatch)
        replies = [r for r in read_jsonl(output / 'model_responses.jsonl') if r['request_id'] in ids]
        evaluation = evaluate_response_records([r for r in requests if r['request_id'] in ids],
            [r for r in oracles if r['request_id'] in ids], replies, run / 'pilot' / kind)['summary']
        truncations = sum(r.get('finish_reason') == 'length' for r in read_jsonl(output / 'api_attempts.jsonl')
                          if r['request_id'] in ids and r.get('status') == 'received')
        gate = {'evaluation': evaluation, 'truncations': truncations,
                'passed': evaluation['response_complete'] and evaluation['format_valid_rate'] >= 0.95 and truncations == 0}
        save(run / 'pilot' / (kind + '_gate.json'), gate)
        if not gate['passed']:
            raise RuntimeError('Engineering pilot did not pass; inspect the saved gate before bulk calls')
    print(json.dumps({k: summary[k] for k in ["kind", "selected_request_count", "response_count", "evaluation"]}), flush=True)


def prepare_repeat(run):
    run = Path(run)
    requests = read_jsonl(run / 'prepared/oracle_state/requests.jsonl')
    by_id = {r['request_id']: r for r in read_jsonl(run / 'prepared/oracle_state/oracles.jsonl')}
    rng = random.Random(20260905)
    chosen = rng.sample(sorted(requests, key=lambda r: r['request_id']), min(40, len(requests)))
    dest = run / 'prepared/repeat_state'
    dest.mkdir(parents=True, exist_ok=True)
    # Same visible messages and oracle, independent calls in a separate response store.
    write_jsonl(dest / 'requests.jsonl', chosen)
    write_jsonl(dest / 'oracles.jsonl', [by_id[r['request_id']] for r in chosen])
    save(dest / 'selection.json', {'seed': 20260905, 'request_ids': [r['request_id'] for r in chosen]})


def predicted(run):
    from granularity3_local.decomposed_prepare import prepare_predicted_state_dataset
    run = Path(run)
    return prepare_predicted_state_dataset(
        read_jsonl(run / "prepared/control_flow/requests.jsonl"),
        read_jsonl(run / "control_flow/model_responses.jsonl"),
        read_jsonl(run / "prepared/oracle_state/requests.jsonl"),
        read_jsonl(run / "prepared/oracle_state/oracles.jsonl"), run / "prepared/predicted_state")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=["build", "prepare", "control_flow", "oracle_state", "predicted_state", "repeat_state"])
    parser.add_argument("--run", default="runs/fresh100_20260906")
    parser.add_argument("--data", default="data")
    parser.add_argument("--model", default="gpt-5.4")
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument('--pilot', action='store_true')
    args = parser.parse_args()
    if args.stage == "build":
        build(args.run, args.data, args.model, args.concurrency)
    elif args.stage == "prepare":
        prepare(args.run, args.concurrency)
    else:
        if args.stage == "predicted_state":
            predicted(args.run)
        if args.stage == 'repeat_state':
            prepare_repeat(args.run)
        infer(args.run, args.stage, args.concurrency, args.pilot)
