"""API runner for decomposed control-flow and variable-state requests."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from granularity3_local.block_state_api import (
    DEFAULT_API_BASE_URL,
    _create_with_hard_timeout,
    create_http_compatible_client,
    create_openai_compatible_client,
)
from granularity3_local.decomposed_core import (
    CONTROL_FLOW_KIND,
    PREDICTED_STATE_KIND,
    STATE_KINDS,
    build_messages,
    compact_json,
    input_sort_key,
    prompt_for_kind,
    read_json,
    read_jsonl,
    response_payload_from_record,
    task_sort_key,
    validate_response,
)
from granularity3_local.decomposed_evaluate import evaluate_response_records
from granularity3_local.oracle import write_json, write_jsonl


API_SCHEMA_VERSION = "g3-decomposed-api-v2"
RUN_CONFIG_SCHEMA_VERSION = "g3-decomposed-api-config-v2"


def _stable_hash(value):
    return hashlib.sha256(compact_json(value).encode("utf-8")).hexdigest()


def _usage_value(usage, name):
    value = getattr(usage, name, None) if usage is not None else None
    return value if isinstance(value, int) else 0


def _append_jsonl(path, row):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
        handle.write("\n")
        handle.flush()


def _nested_usage_value(usage, container_name, value_name):
    container = getattr(usage, container_name, None) if usage is not None else None
    value = getattr(container, value_name, None) if container is not None else None
    return value if isinstance(value, int) else 0


def generation_config(
    kind,
    model,
    api_base_url,
    max_completion_tokens,
    reasoning_effort,
    verbosity,
    temperature,
):
    return {
        "kind": kind,
        "model": model,
        "api_base_url": api_base_url.rstrip("/"),
        "system_prompt_sha256": hashlib.sha256(
            prompt_for_kind(kind).encode("utf-8")
        ).hexdigest(),
        "max_completion_tokens": max_completion_tokens,
        "reasoning_effort": reasoning_effort,
        "verbosity": verbosity,
        "temperature": temperature,
    }


def request_fingerprint(request_record, config):
    return _stable_hash({
        "request_id": request_record["request_id"],
        "kind": request_record["kind"],
        "request": request_record["request"],
        "generation": config,
    })


def select_requests(
    requests,
    oracles,
    task_ids=None,
    task_limit=None,
    cases_per_task=None,
    max_requests=None,
    request_ids=None,
):
    oracle_by_id = {row["request_id"]: row for row in oracles}
    if len(oracle_by_id) != len(oracles):
        raise ValueError("duplicate oracle request ids")
    request_by_id = {row["request_id"]: row for row in requests}
    if len(request_by_id) != len(requests):
        raise ValueError("duplicate request ids")
    requested_ids = list(dict.fromkeys(request_ids or []))
    if requested_ids:
        if (
            task_ids
            or task_limit is not None
            or cases_per_task is not None
            or max_requests is not None
        ):
            raise ValueError(
                "request_ids cannot be combined with task/case/request limits"
            )
        missing = [request_id for request_id in requested_ids if request_id not in request_by_id]
        if missing:
            raise ValueError(f"request ids not found: {missing[:20]}")
        selected = [request_by_id[request_id] for request_id in requested_ids]
        selected_tasks = list(dict.fromkeys(row["task_id"] for row in selected))
        selected_oracles = []
        for row in selected:
            request_id = row["request_id"]
            if request_id not in oracle_by_id:
                raise ValueError(f"request has no oracle: {request_id}")
            selected_oracles.append(oracle_by_id[request_id])
        return selected, selected_oracles, selected_tasks

    available_tasks = sorted(
        {row["task_id"] for row in requests},
        key=task_sort_key,
    )
    if task_ids:
        selected_tasks = list(dict.fromkeys(task_ids))
        missing = [task for task in selected_tasks if task not in available_tasks]
        if missing:
            raise ValueError(f"tasks not found in requests: {missing}")
    else:
        selected_tasks = available_tasks
    if task_limit is not None:
        if task_limit < 1:
            raise ValueError("task_limit must be positive")
        selected_tasks = selected_tasks[:task_limit]
    selected_task_set = set(selected_tasks)
    selected = [row for row in requests if row["task_id"] in selected_task_set]

    if cases_per_task is not None:
        if cases_per_task < 1:
            raise ValueError("cases_per_task must be positive")
        allowed_cases = set()
        for task_id in selected_tasks:
            case_keys = sorted(
                {row["case_key"] for row in selected if row["task_id"] == task_id},
                key=lambda key: input_sort_key(key.split("/", 1)[1]),
            )[:cases_per_task]
            allowed_cases.update(case_keys)
        selected = [row for row in selected if row["case_key"] in allowed_cases]

    if max_requests is not None:
        if max_requests < 1:
            raise ValueError("max_requests must be positive")
        selected = selected[:max_requests]

    selected_oracles = []
    for row in selected:
        request_id = row["request_id"]
        if request_id not in oracle_by_id:
            raise ValueError(f"request has no oracle: {request_id}")
        selected_oracles.append(oracle_by_id[request_id])
    return selected, selected_oracles, selected_tasks


def call_one_request(
    client,
    request_record,
    model,
    timeout,
    max_completion_tokens,
    retries,
    retry_invalid,
    reasoning_effort,
    verbosity,
    temperature,
    config,
    fingerprint,
    on_attempt=None,
):
    attempts = []
    final_response = None
    for attempt in range(1, retries + 2):
        started = time.perf_counter()
        try:
            kwargs = {
                "model": model,
                "messages": build_messages(request_record),
                "timeout": timeout,
            }
            if max_completion_tokens is not None:
                kwargs["max_completion_tokens"] = max_completion_tokens
            if reasoning_effort is not None:
                kwargs["reasoning_effort"] = reasoning_effort
            if verbosity is not None:
                kwargs["verbosity"] = verbosity
            if temperature is not None:
                kwargs["temperature"] = temperature
            response = _create_with_hard_timeout(
                client.chat.completions.create,
                kwargs,
                timeout,
            )
            content = response.choices[0].message.content or ""
            usage = getattr(response, "usage", None)
            attempt_record = {
                "request_id": request_record["request_id"],
                "request_fingerprint": fingerprint,
                "generation_config": config,
                "attempt": attempt,
                "status": "received",
                "elapsed_seconds": time.perf_counter() - started,
                "finish_reason": getattr(response.choices[0], "finish_reason", None),
                "response_model": getattr(response, "model", None),
                "request_id_from_api": getattr(response, "_request_id", None),
                "prompt_tokens": _usage_value(usage, "prompt_tokens"),
                "completion_tokens": _usage_value(usage, "completion_tokens"),
                "total_tokens": _usage_value(usage, "total_tokens"),
                "reasoning_tokens": _nested_usage_value(
                    usage,
                    "completion_tokens_details",
                    "reasoning_tokens",
                ),
                "cached_prompt_tokens": _nested_usage_value(
                    usage,
                    "prompt_tokens_details",
                    "cached_tokens",
                ),
                "raw_response": content,
            }
            try:
                validate_response(request_record, content)
                attempt_record["validation"] = "valid"
                final_response = {
                    "request_id": request_record["request_id"],
                    "request_fingerprint": fingerprint,
                    "raw_response": content,
                }
                attempts.append(attempt_record)
                if on_attempt:
                    on_attempt(attempt_record)
                break
            except Exception as error:
                attempt_record["validation"] = "invalid"
                attempt_record["validation_error"] = str(error)
                final_response = {
                    "request_id": request_record["request_id"],
                    "request_fingerprint": fingerprint,
                    "raw_response": content,
                }
                attempts.append(attempt_record)
                if on_attempt:
                    on_attempt(attempt_record)
                if not retry_invalid:
                    break
        except Exception as error:
            attempt_record = {
                "request_id": request_record["request_id"],
                "request_fingerprint": fingerprint,
                "generation_config": config,
                "attempt": attempt,
                "status": "api_error",
                "elapsed_seconds": time.perf_counter() - started,
                "error_type": type(error).__name__,
                "reason": str(error),
            }
            attempts.append(attempt_record)
            if on_attempt:
                on_attempt(attempt_record)
        if attempt <= retries:
            time.sleep(min(2 ** (attempt - 1), 4))
    return final_response, attempts


def _load_responses(path, request_by_id, fingerprints, reuse_received):
    reusable = {}
    for row in read_jsonl(path):
        request_id = row.get("request_id")
        if request_id not in request_by_id:
            continue
        if row.get("request_fingerprint") != fingerprints[request_id]:
            continue
        if reuse_received:
            reusable[request_id] = row
            continue
        try:
            payload = response_payload_from_record(row)
            validate_response(request_by_id[request_id], payload)
        except Exception:
            continue
        reusable[request_id] = row
    return reusable


def run_api_experiment(
    client,
    requests,
    oracles,
    output_dir,
    model,
    api_base_url,
    task_ids=None,
    task_limit=None,
    cases_per_task=None,
    max_requests=None,
    request_ids=None,
    timeout=180,
    max_completion_tokens=16000,
    retries=1,
    retry_invalid=False,
    reasoning_effort=None,
    verbosity=None,
    temperature=None,
    concurrency=1,
    resume=False,
    resume_received=False,
    progress_every=1,
):
    if concurrency < 1:
        raise ValueError("concurrency must be positive")
    if progress_every < 1:
        raise ValueError("progress_every must be positive")
    selected, selected_oracles, selected_tasks = select_requests(
        requests,
        oracles,
        task_ids=task_ids,
        task_limit=task_limit,
        cases_per_task=cases_per_task,
        max_requests=max_requests,
        request_ids=request_ids,
    )
    if not selected:
        raise ValueError("selection contains no requests")
    kinds = {row["kind"] for row in selected}
    if len(kinds) != 1:
        raise ValueError(f"API run requires one task kind, got {sorted(kinds)}")
    kind = next(iter(kinds))
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    config = generation_config(
        kind=kind,
        model=model,
        api_base_url=api_base_url,
        max_completion_tokens=max_completion_tokens,
        reasoning_effort=reasoning_effort,
        verbosity=verbosity,
        temperature=temperature,
    )
    fingerprints = {
        row["request_id"]: request_fingerprint(row, config)
        for row in selected
    }
    run_config = {
        "schema_version": RUN_CONFIG_SCHEMA_VERSION,
        "kind": kind,
        "selected_tasks": selected_tasks,
        "selected_request_count": len(selected),
        "selection": {
            "task_ids": task_ids,
            "task_limit": task_limit,
            "cases_per_task": cases_per_task,
            "max_requests": max_requests,
            "request_ids_file_count": len(request_ids or []),
            "ordered_request_ids_sha256": _stable_hash(
                [row["request_id"] for row in selected]
            ),
        },
        "generation": config,
        "execution": {
            "timeout": timeout,
            "retries": retries,
            "retry_invalid": retry_invalid,
            "concurrency": concurrency,
            "progress_every": progress_every,
        },
    }
    run_config["fingerprint"] = _stable_hash(run_config)
    config_path = output_dir / "run_config.json"
    responses_path = output_dir / "model_responses.jsonl"
    attempts_path = output_dir / "api_attempts.jsonl"
    if config_path.exists():
        existing = read_json(config_path)
        if existing.get("fingerprint") != run_config["fingerprint"]:
            raise ValueError("output directory contains a different run configuration")
        if not resume:
            raise ValueError("output directory already exists; use --resume or a new directory")
    elif responses_path.exists() or attempts_path.exists():
        raise ValueError("output directory has API artifacts but no run configuration")

    write_json(config_path, run_config)
    write_jsonl(output_dir / "selected_requests.jsonl", selected)
    write_jsonl(output_dir / "selected_oracles.jsonl", selected_oracles)
    request_by_id = {row["request_id"]: row for row in selected}
    response_by_id = (
        _load_responses(
            responses_path,
            request_by_id,
            fingerprints,
            reuse_received=resume_received,
        )
        if resume
        else {}
    )
    attempts = read_jsonl(attempts_path) if resume else []
    lock = threading.Lock()

    def persist_attempt(row):
        with lock:
            attempts.append(row)
            _append_jsonl(attempts_path, row)

    def persist_response(request_id, response):
        with lock:
            if response is not None:
                response_by_id[request_id] = response
                _append_jsonl(responses_path, response)

    started = time.perf_counter()

    def invoke(index, request_record):
        request_id = request_record["request_id"]
        show_progress = (
            index == 1
            or index == len(selected)
            or index % progress_every == 0
        )
        if show_progress:
            print(
                f"[decomposed-api] start {index}/{len(selected)} {request_id}",
                flush=True,
            )
        response, _ = call_one_request(
            client=client,
            request_record=request_record,
            model=model,
            timeout=timeout,
            max_completion_tokens=max_completion_tokens,
            retries=retries,
            retry_invalid=retry_invalid,
            reasoning_effort=reasoning_effort,
            verbosity=verbosity,
            temperature=temperature,
            config=config,
            fingerprint=fingerprints[request_id],
            on_attempt=persist_attempt,
        )
        persist_response(request_id, response)
        if show_progress or response is None:
            print(
                f"[decomposed-api] done {index}/{len(selected)} {request_id} "
                f"received={response is not None}",
                flush=True,
            )
        return request_id

    pending = [
        (index, row)
        for index, row in enumerate(selected, start=1)
        if row["request_id"] not in response_by_id
    ]
    if concurrency == 1:
        for index, row in pending:
            invoke(index, row)
    else:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = [pool.submit(invoke, index, row) for index, row in pending]
            for future in as_completed(futures):
                future.result()

    responses = [
        response_by_id[row["request_id"]]
        for row in selected
        if row["request_id"] in response_by_id
    ]
    write_jsonl(responses_path, responses)
    write_jsonl(attempts_path, attempts)
    evaluation = evaluate_response_records(
        selected,
        selected_oracles,
        responses,
        output_dir / "evaluation",
    )
    received_attempts = [row for row in attempts if row.get("status") == "received"]
    summary = {
        "schema_version": API_SCHEMA_VERSION,
        "run_config_fingerprint": run_config["fingerprint"],
        "kind": kind,
        "model": model,
        "selected_task_count": len(selected_tasks),
        "selected_tasks": selected_tasks,
        "selected_request_count": len(selected),
        "response_count": len(responses),
        "resumed_response_count": len(selected) - len(pending),
        "api_error_count": sum(row.get("status") == "api_error" for row in attempts),
        "invalid_attempt_count": sum(
            row.get("validation") == "invalid" for row in received_attempts
        ),
        "prompt_tokens": sum(row.get("prompt_tokens", 0) for row in received_attempts),
        "completion_tokens": sum(
            row.get("completion_tokens", 0) for row in received_attempts
        ),
        "reasoning_tokens": sum(
            row.get("reasoning_tokens", 0) for row in received_attempts
        ),
        "elapsed_seconds": time.perf_counter() - started,
        "evaluation": evaluation["summary"],
    }
    write_json(output_dir / "summary.json", summary)
    return summary


def _parse_tasks(value):
    if not value:
        return None
    return [item.strip() for item in value.split(",") if item.strip()]


def _read_request_ids(path):
    if not path:
        return None
    result = []
    for line_number, line in enumerate(
        Path(path).read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        line = line.strip()
        if not line:
            continue
        if line.startswith("{"):
            value = json.loads(line)
            request_id = value.get("request_id") if isinstance(value, dict) else None
        else:
            request_id = line
        if not isinstance(request_id, str) or not request_id:
            raise ValueError(f"invalid request id at {path}:{line_number}")
        result.append(request_id)
    return result


def main():
    parser = argparse.ArgumentParser(
        description="Run decomposed granularity-3 requests through a Chat Completions API."
    )
    parser.add_argument("--requests", required=True)
    parser.add_argument("--oracles", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument(
        "--kind",
        required=True,
        choices=(CONTROL_FLOW_KIND, *sorted(STATE_KINDS)),
    )
    parser.add_argument("--tasks", help="Comma-separated task ids.")
    parser.add_argument("--task-limit", type=int)
    parser.add_argument("--cases-per-task", type=int)
    parser.add_argument("--max-requests", type=int)
    parser.add_argument(
        "--request-ids-file",
        help="Optional newline/JSONL file selecting an exact frozen request cohort.",
    )
    parser.add_argument("--model", default=os.getenv("YUNWU_MODEL", ""))
    parser.add_argument(
        "--base-url",
        default=os.getenv("YUNWU_API_BASE_URL", DEFAULT_API_BASE_URL),
    )
    parser.add_argument("--transport", choices=("sdk", "http"), default="sdk")
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--max-completion-tokens", type=int, default=16000)
    parser.add_argument("--retries", type=int, default=1)
    parser.add_argument("--retry-invalid", action="store_true")
    parser.add_argument(
        "--reasoning-effort",
        choices=("none", "low", "medium", "high", "xhigh"),
    )
    parser.add_argument("--verbosity", choices=("low", "medium", "high"))
    parser.add_argument("--temperature", type=float)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--progress-every", type=int, default=1)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--resume-received", action="store_true")
    args = parser.parse_args()

    if args.resume_received and not args.resume:
        raise SystemExit("--resume-received requires --resume")
    if args.reasoning_effort not in (None, "none") and args.temperature is not None:
        raise SystemExit("omit --temperature when reasoning_effort is above none")
    api_key = os.getenv("YUNWU_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("missing YUNWU_API_KEY environment variable")
    if not args.model.strip():
        raise SystemExit("missing YUNWU_MODEL environment variable or --model")

    requests = read_jsonl(args.requests)
    oracles = read_jsonl(args.oracles)
    if any(row.get("kind") != args.kind for row in requests):
        raise SystemExit("--kind does not match every request record")
    if args.transport == "http":
        client = create_http_compatible_client(api_key, args.base_url)
    else:
        client = create_openai_compatible_client(api_key, args.base_url)
    summary = run_api_experiment(
        client=client,
        requests=requests,
        oracles=oracles,
        output_dir=args.output_dir,
        model=args.model,
        api_base_url=args.base_url,
        task_ids=_parse_tasks(args.tasks),
        task_limit=args.task_limit,
        cases_per_task=args.cases_per_task,
        max_requests=args.max_requests,
        request_ids=_read_request_ids(args.request_ids_file),
        timeout=args.timeout,
        max_completion_tokens=args.max_completion_tokens,
        retries=args.retries,
        retry_invalid=args.retry_invalid,
        reasoning_effort=args.reasoning_effort,
        verbosity=args.verbosity,
        temperature=args.temperature,
        concurrency=args.concurrency,
        resume=args.resume,
        resume_received=args.resume_received,
        progress_every=args.progress_every,
    )
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
