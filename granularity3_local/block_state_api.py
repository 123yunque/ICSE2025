import argparse
import hashlib
import json
import math
import os
import queue
import threading
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from types import SimpleNamespace

from granularity3_local.block_state_batch import (
    FLAT_RUN_SYSTEM_PROMPT,
    SINGLE_CASE_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    read_jsonl,
)
from granularity3_local.block_state_evaluate import (
    ResponseValidationError,
    attach_and_validate_response,
    evaluate_response_records,
)
from granularity3_local.block_state_local import build_flat_run_answer
from granularity3_local.oracle import write_json, write_jsonl


API_RUN_SCHEMA_VERSION = "g3-block-state-api-run-v2"
RUN_CONFIG_SCHEMA_VERSION = "g3-block-state-api-config-v2"
REQUEST_FINGERPRINT_SCHEMA_VERSION = "g3-block-state-request-fingerprint-v2"
DEFAULT_API_BASE_URL = "https://api.openlux.ai/v1"


def create_openai_compatible_client(api_key, base_url):
    """Support both current and legacy OpenAI Python clients available in this project."""
    try:
        from openai import OpenAI
    except ImportError:
        import openai

        openai.api_key = api_key
        openai.api_base = base_url

        class LegacyCompletions:
            @staticmethod
            def create(**kwargs):
                timeout = kwargs.pop("timeout", None)
                if timeout is not None:
                    kwargs["request_timeout"] = timeout
                return openai.ChatCompletion.create(**kwargs)

        class LegacyChat:
            completions = LegacyCompletions()

        class LegacyClient:
            chat = LegacyChat()

        return LegacyClient()
    return OpenAI(api_key=api_key, base_url=base_url, max_retries=0)


def _as_namespace(value):
    if isinstance(value, dict):
        return SimpleNamespace(**{key: _as_namespace(item) for key, item in value.items()})
    if isinstance(value, list):
        return [_as_namespace(item) for item in value]
    return value


def create_http_compatible_client(api_key, base_url):
    """Small dependency-free Chat Completions client for environments with an old SDK."""
    endpoint = base_url.rstrip("/") + "/chat/completions"

    class HttpCompletions:
        @staticmethod
        def create(**kwargs):
            timeout = kwargs.pop("timeout", None)
            request = urllib.request.Request(
                endpoint,
                data=json.dumps(kwargs, ensure_ascii=False).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "X-Client-Request-Id": str(uuid.uuid4()),
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                    result = _as_namespace(payload)
                    result._request_id = response.headers.get("x-request-id")
                    return result
            except urllib.error.HTTPError as error:
                body = error.read().decode("utf-8", errors="replace")
                raise RuntimeError(f"HTTP {error.code}: {body}") from error
            except urllib.error.URLError as error:
                raise RuntimeError(f"API connection failed: {error.reason}") from error

    class HttpChat:
        completions = HttpCompletions()

    class HttpClient:
        chat = HttpChat()

    return HttpClient()


def task_id_from_batch(batch_id):
    return batch_id.split("/", 1)[0]


def select_task_batches(model_batches, oracle_batches, task_ids):
    requested = list(dict.fromkeys(task_ids))
    requested_set = set(requested)
    selected_models = [
        row for row in model_batches if task_id_from_batch(row["batch_id"]) in requested_set
    ]
    selected_oracles = [
        row for row in oracle_batches if task_id_from_batch(row["batch_id"]) in requested_set
    ]
    found = {task_id_from_batch(row["batch_id"]) for row in selected_models}
    missing = [task_id for task_id in requested if task_id not in found]
    if missing:
        raise ValueError(f"tasks not found in model batches: {missing}")
    oracle_ids = {row["batch_id"] for row in selected_oracles}
    model_ids = {row["batch_id"] for row in selected_models}
    if model_ids != oracle_ids:
        raise ValueError("selected model and Oracle batch ids differ")
    order = {task_id: index for index, task_id in enumerate(requested)}
    selected_models.sort(key=lambda row: (order[task_id_from_batch(row["batch_id"])], row["batch_id"]))
    oracle_by_id = {row["batch_id"]: row for row in selected_oracles}
    selected_oracles = [oracle_by_id[row["batch_id"]] for row in selected_models]
    return selected_models, selected_oracles


def split_selected_batches(model_batches, oracle_batches, max_cases_per_batch):
    if max_cases_per_batch is None:
        return model_batches, oracle_batches
    if max_cases_per_batch < 1:
        raise ValueError("max_cases_per_batch must be positive")
    oracle_by_id = {row["batch_id"]: row for row in oracle_batches}
    split_models = []
    split_oracles = []
    for model_row in model_batches:
        batch_id = model_row["batch_id"]
        cases = model_row["request"]["cases"]
        oracle_results = oracle_by_id[batch_id]["results"]
        oracle_cases = {row["id"]: row for row in oracle_results}
        parts = [cases[index:index + max_cases_per_batch] for index in range(0, len(cases), max_cases_per_batch)]
        for part_index, part in enumerate(parts, start=1):
            part_id = batch_id if len(parts) == 1 else f"{batch_id}_part_{part_index}"
            request = dict(model_row["request"])
            request["cases"] = part
            split_models.append({"batch_id": part_id, "request": request})
            split_oracles.append({
                "batch_id": part_id,
                "results": [oracle_cases[case["id"]] for case in part],
            })
    return split_models, split_oracles


def split_one_case_per_request(model_batches, oracle_batches, response_format="single_case"):
    """Expand task batches into stable, one-input API requests."""
    oracle_by_id = {row["batch_id"]: row for row in oracle_batches}
    split_models = []
    split_oracles = []
    seen_ids = set()
    for model_row in model_batches:
        parent_batch_id = model_row["batch_id"]
        cases = model_row["request"].get("cases", [])
        oracle_results = oracle_by_id[parent_batch_id]["results"]
        oracle_cases = {row["id"]: row for row in oracle_results}
        task_id = task_id_from_batch(parent_batch_id)
        for case in cases:
            case_id = case["id"]
            request_id = f"{task_id}/{case_id}"
            if request_id in seen_ids:
                raise ValueError(f"duplicate one-case request id: {request_id}")
            if case_id not in oracle_cases:
                raise ValueError(f"missing Oracle result for {parent_batch_id}/{case_id}")
            seen_ids.add(request_id)
            request = dict(model_row["request"])
            request["cases"] = [case]
            oracle_result = oracle_cases[case_id]
            if response_format == "flat_runs":
                flat_answer = build_flat_run_answer(
                    {
                        "block_trace": oracle_result["block_trace"],
                        "changes": oracle_result["changes"],
                    },
                    request["blocks"],
                )
                oracle_result = {"id": case_id, **flat_answer}
            split_models.append({
                "batch_id": request_id,
                "parent_batch_id": parent_batch_id,
                "response_format": response_format,
                "request": request,
            })
            split_oracles.append({
                "batch_id": request_id,
                "parent_batch_id": parent_batch_id,
                "response_format": response_format,
                "results": [oracle_result],
            })
    return split_models, split_oracles


def build_messages(request, response_format="batch"):
    return [
        {
            "role": "system",
            "content": system_prompt_for(response_format),
        },
        {
            "role": "user",
            "content": json.dumps(request, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        },
    ]


def system_prompt_for(response_format):
    prompts = {
        "batch": SYSTEM_PROMPT,
        "single_case": SINGLE_CASE_SYSTEM_PROMPT,
        "flat_runs": FLAT_RUN_SYSTEM_PROMPT,
    }
    if response_format not in prompts:
        raise ValueError(f"unknown response format: {response_format}")
    return prompts[response_format]


def _usage_value(usage, name):
    value = getattr(usage, name, None) if usage is not None else None
    return value if isinstance(value, int) else 0


def _nested_usage_value(usage, container_name, value_name):
    container = getattr(usage, container_name, None) if usage is not None else None
    value = getattr(container, value_name, None) if container is not None else None
    return value if isinstance(value, int) else 0


def _nested_usage_present(usage, container_name, value_name):
    container = getattr(usage, container_name, None) if usage is not None else None
    return isinstance(
        getattr(container, value_name, None) if container is not None else None,
        int,
    )


def build_generation_config(
    model,
    api_base_url,
    response_format,
    max_completion_tokens,
    reasoning_effort=None,
    verbosity=None,
    temperature=0,
):
    prompt = system_prompt_for(response_format)
    return {
        "schema_version": REQUEST_FINGERPRINT_SCHEMA_VERSION,
        "model": model,
        "api_base_url": api_base_url.rstrip("/") if api_base_url else None,
        "response_format": response_format,
        "system_prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "max_completion_tokens": max_completion_tokens,
        "reasoning_effort": reasoning_effort,
        "verbosity": verbosity,
        "temperature": temperature,
    }


def request_fingerprint(request, response_format="batch", generation_config=None):
    payload = json.dumps(
        {
            "schema_version": REQUEST_FINGERPRINT_SCHEMA_VERSION,
            "request": request,
            "response_format": response_format,
            "generation_config": generation_config,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _stable_hash(value):
    payload = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _percentile(values, percentile):
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def _response_is_valid(request_record, response_record, expected_fingerprint=None):
    """Return whether a persisted response can safely be reused on resume."""
    if not _response_matches_request(
        request_record,
        response_record,
        expected_fingerprint=expected_fingerprint,
    ):
        return False
    if "raw_response" in response_record:
        payload = response_record["raw_response"]
    elif "response" in response_record:
        payload = response_record["response"]
    elif "results" in response_record:
        payload = response_record
    else:
        return False
    try:
        attach_and_validate_response(request_record, payload)
    except (ResponseValidationError, ValueError, KeyError, TypeError):
        return False
    return True


def _response_matches_request(
    request_record,
    response_record,
    expected_fingerprint=None,
):
    """Return whether a persisted response belongs to this exact request format."""
    if not isinstance(response_record, dict):
        return False
    if expected_fingerprint is None:
        expected_fingerprint = request_fingerprint(
            request_record["request"],
            request_record.get("response_format", "batch"),
        )
    if response_record.get("request_fingerprint") != expected_fingerprint:
        return False
    return any(key in response_record for key in ("raw_response", "response", "results"))


def _load_resumable_responses(
    path,
    selected_models,
    expected_fingerprints=None,
    reuse_received=False,
):
    requests = {row["batch_id"]: row for row in selected_models}
    expected_fingerprints = expected_fingerprints or {}
    latest = {}
    for row in read_jsonl(path):
        batch_id = row.get("batch_id") if isinstance(row, dict) else None
        if batch_id in requests:
            latest[batch_id] = row
    valid_ids = {
        batch_id
        for batch_id, row in latest.items()
        if (
            _response_matches_request(
                requests[batch_id],
                row,
                expected_fingerprint=expected_fingerprints.get(batch_id),
            )
            if reuse_received
            else _response_is_valid(
                requests[batch_id],
                row,
                expected_fingerprint=expected_fingerprints.get(batch_id),
            )
        )
    }
    return latest, valid_ids


def _create_with_hard_timeout(create, kwargs, timeout):
    """Enforce a wall-clock deadline even if a transport keeps receiving partial data."""
    if timeout is None:
        return create(**kwargs)

    result_queue = queue.Queue(maxsize=1)

    def worker():
        try:
            result_queue.put(("response", create(**kwargs)))
        except BaseException as error:  # Propagate the provider/transport exception.
            result_queue.put(("error", error))

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise TimeoutError(f"API call exceeded hard timeout of {timeout:g} seconds")
    status, value = result_queue.get_nowait()
    if status == "error":
        raise value
    return value


def call_one_batch(
    client,
    batch_record,
    model,
    timeout,
    max_completion_tokens,
    retries,
    retry_invalid=False,
    on_attempt=None,
    reasoning_effort=None,
    verbosity=None,
    temperature=0,
    generation_config=None,
    expected_fingerprint=None,
):
    attempts = []
    final_response = None

    def record_attempt(row):
        attempts.append(row)
        if on_attempt is not None:
            on_attempt(row)

    response_format = batch_record.get("response_format", "batch")
    if generation_config is None:
        generation_config = build_generation_config(
            model=model,
            api_base_url=None,
            response_format=response_format,
            max_completion_tokens=max_completion_tokens,
            reasoning_effort=reasoning_effort,
            verbosity=verbosity,
            temperature=temperature,
        )
    if expected_fingerprint is None:
        expected_fingerprint = request_fingerprint(
            batch_record["request"],
            response_format,
            generation_config=generation_config,
        )

    for attempt in range(1, retries + 2):
        started = time.perf_counter()
        try:
            kwargs = {
                "model": model,
                "messages": build_messages(
                    batch_record["request"],
                    response_format=response_format,
                ),
                "timeout": timeout,
            }
            if temperature is not None:
                kwargs["temperature"] = temperature
            if max_completion_tokens is not None:
                kwargs["max_completion_tokens"] = max_completion_tokens
            if reasoning_effort is not None:
                kwargs["reasoning_effort"] = reasoning_effort
            if verbosity is not None:
                kwargs["verbosity"] = verbosity
            response = _create_with_hard_timeout(
                client.chat.completions.create,
                kwargs,
                timeout,
            )
            elapsed = time.perf_counter() - started
            content = response.choices[0].message.content or ""
            usage = getattr(response, "usage", None)
            attempt_record = {
                "batch_id": batch_record["batch_id"],
                "request_fingerprint": expected_fingerprint,
                "generation_config": generation_config,
                "request_timeout_seconds": timeout,
                "attempt": attempt,
                "status": "received",
                "elapsed_seconds": elapsed,
                "finish_reason": getattr(response.choices[0], "finish_reason", None),
                "response_model": getattr(response, "model", None),
                "request_id": getattr(response, "_request_id", None),
                "prompt_tokens": _usage_value(usage, "prompt_tokens"),
                "completion_tokens": _usage_value(usage, "completion_tokens"),
                "total_tokens": _usage_value(usage, "total_tokens"),
                "reasoning_tokens": _nested_usage_value(
                    usage,
                    "completion_tokens_details",
                    "reasoning_tokens",
                ),
                "reasoning_tokens_reported": _nested_usage_present(
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
                attach_and_validate_response(batch_record, content)
                attempt_record["validation"] = "valid"
                record_attempt(attempt_record)
                final_response = {
                    "batch_id": batch_record["batch_id"],
                    "request_fingerprint": expected_fingerprint,
                    "raw_response": content,
                }
                break
            except ResponseValidationError as error:
                attempt_record["validation"] = "invalid"
                attempt_record["validation_error"] = str(error)
                record_attempt(attempt_record)
                final_response = {
                    "batch_id": batch_record["batch_id"],
                    "request_fingerprint": expected_fingerprint,
                    "raw_response": content,
                }
                if not retry_invalid:
                    break
        except Exception as error:
            record_attempt({
                "batch_id": batch_record["batch_id"],
                "request_fingerprint": expected_fingerprint,
                "generation_config": generation_config,
                "request_timeout_seconds": timeout,
                "attempt": attempt,
                "status": "api_error",
                "elapsed_seconds": time.perf_counter() - started,
                "error_type": type(error).__name__,
                "reason": str(error),
            })
        if attempt <= retries:
            time.sleep(min(2 ** (attempt - 1), 4))
    return final_response, attempts


def select_case_keys(model_batches, oracle_batches, case_keys):
    """Keep an explicit ordered subset after one-case requests have been created."""
    requested = list(dict.fromkeys(case_keys))
    model_by_id = {row["batch_id"]: row for row in model_batches}
    oracle_by_id = {row["batch_id"]: row for row in oracle_batches}
    missing = [case_key for case_key in requested if case_key not in model_by_id]
    if missing:
        raise ValueError(f"case keys not found after request splitting: {missing}")
    missing_oracles = [case_key for case_key in requested if case_key not in oracle_by_id]
    if missing_oracles:
        raise ValueError(f"case keys have no Oracle record: {missing_oracles}")
    return (
        [model_by_id[case_key] for case_key in requested],
        [oracle_by_id[case_key] for case_key in requested],
    )


def loop_header_count(batch_record):
    count = 0
    for _block_id, _source, outgoing_edges in batch_record["request"].get("blocks", []):
        if any(edge[0] == "loop_body" for edge in outgoing_edges):
            count += 1
    return count


def timeout_for_batch(
    batch_record,
    timeout,
    complex_timeout=None,
    complex_loop_threshold=2,
):
    loop_count = loop_header_count(batch_record)
    if complex_timeout is not None and loop_count >= complex_loop_threshold:
        return complex_timeout, loop_count
    return timeout, loop_count


def _read_json_if_exists(path):
    path = Path(path)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _assert_output_config_compatible(output_dir, run_config, resume):
    config_path = Path(output_dir) / "run_config.json"
    existing = _read_json_if_exists(config_path)
    response_path = Path(output_dir) / "model_responses.jsonl"
    has_responses = response_path.exists() and response_path.stat().st_size > 0
    if existing is None:
        if resume and has_responses:
            raise ValueError(
                "cannot safely resume responses created before run_config.json; "
                "use a new output directory"
            )
        if not resume and has_responses:
            raise ValueError(
                "output directory already contains responses without run_config.json; "
                "use --resume with a compatible run or choose a new output directory"
            )
        return
    if existing != run_config:
        raise ValueError(
            "output directory run configuration differs; refusing to mix model, "
            "prompt, generation, timeout, concurrency, or selection settings"
        )
    if has_responses and not resume:
        raise ValueError(
            "output directory already contains responses; use --resume or a new output directory"
        )


def run_api_experiment(
    client,
    model_batches,
    oracle_batches,
    task_ids,
    model,
    output_dir,
    timeout=180,
    max_completion_tokens=16000,
    retries=1,
    max_cases_per_batch=None,
    retry_invalid=False,
    one_case_per_request=False,
    flat_runs=False,
    max_requests=None,
    resume=False,
    resume_received=False,
    api_base_url=None,
    reasoning_effort=None,
    verbosity=None,
    temperature=0,
    concurrency=1,
    complex_timeout=None,
    complex_loop_threshold=2,
    case_keys=None,
    stage_case_keys=None,
):
    if concurrency < 1:
        raise ValueError("concurrency must be positive")
    if complex_loop_threshold < 1:
        raise ValueError("complex_loop_threshold must be positive")
    if max_requests is not None and case_keys:
        raise ValueError("max_requests and case_keys cannot be used together")
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    selected_models, selected_oracles = select_task_batches(
        model_batches, oracle_batches, task_ids
    )
    if flat_runs:
        selected_models, selected_oracles = split_one_case_per_request(
            selected_models, selected_oracles, response_format="flat_runs"
        )
    elif one_case_per_request:
        selected_models, selected_oracles = split_one_case_per_request(
            selected_models, selected_oracles
        )
    else:
        selected_models, selected_oracles = split_selected_batches(
            selected_models, selected_oracles, max_cases_per_batch
        )
    if case_keys:
        selected_models, selected_oracles = select_case_keys(
            selected_models,
            selected_oracles,
            case_keys,
        )
    if max_requests is not None:
        if max_requests < 1:
            raise ValueError("max_requests must be positive")
        selected_models = selected_models[:max_requests]
        selected_oracles = selected_oracles[:max_requests]

    stage_ids = None
    if stage_case_keys:
        requested_stage_keys = list(dict.fromkeys(stage_case_keys))
        planned_ids = {row["batch_id"] for row in selected_models}
        missing_stage_keys = [
            case_key for case_key in requested_stage_keys if case_key not in planned_ids
        ]
        if missing_stage_keys:
            raise ValueError(
                f"stage case keys are outside the fixed run plan: {missing_stage_keys}"
            )
        stage_ids = set(requested_stage_keys)

    generation_configs = {}
    expected_fingerprints = {}
    for row in selected_models:
        batch_id = row["batch_id"]
        response_format = row.get("response_format", "batch")
        generation_config = build_generation_config(
            model=model,
            api_base_url=api_base_url,
            response_format=response_format,
            max_completion_tokens=max_completion_tokens,
            reasoning_effort=reasoning_effort,
            verbosity=verbosity,
            temperature=temperature,
        )
        generation_configs[batch_id] = generation_config
        expected_fingerprints[batch_id] = request_fingerprint(
            row["request"],
            response_format,
            generation_config=generation_config,
        )

    run_config = {
        "schema_version": RUN_CONFIG_SCHEMA_VERSION,
        "model": model,
        "api_base_url": api_base_url.rstrip("/") if api_base_url else None,
        "tasks": list(task_ids),
        "selection": {
            "selected_batch_count": len(selected_models),
            "selected_case_count": sum(
                len(row["request"]["cases"]) for row in selected_models
            ),
            "ordered_batch_ids_sha256": _stable_hash(
                [row["batch_id"] for row in selected_models]
            ),
            "ordered_request_fingerprints_sha256": _stable_hash(
                [expected_fingerprints[row["batch_id"]] for row in selected_models]
            ),
            "case_keys": list(case_keys) if case_keys else None,
            "max_requests": max_requests,
            "max_cases_per_batch": max_cases_per_batch,
            "one_case_per_request": bool(one_case_per_request or flat_runs),
            "answer_format": "flat_runs" if flat_runs else "expanded",
        },
        "generation": {
            "reasoning_effort": reasoning_effort,
            "verbosity": verbosity,
            "temperature": temperature,
            "max_completion_tokens": max_completion_tokens,
            "prompt_sha256_by_format": {
                response_format: hashlib.sha256(
                    system_prompt_for(response_format).encode("utf-8")
                ).hexdigest()
                for response_format in sorted({
                    row.get("response_format", "batch") for row in selected_models
                })
            },
        },
        "execution": {
            "timeout_seconds": timeout,
            "complex_timeout_seconds": complex_timeout,
            "complex_loop_threshold": complex_loop_threshold,
            "concurrency": concurrency,
            "retries": retries,
            "retry_invalid": retry_invalid,
        },
    }
    run_config["run_config_fingerprint"] = _stable_hash(run_config)
    _assert_output_config_compatible(output_dir, run_config, resume)
    write_json(output_dir / "run_config.json", run_config)
    write_jsonl(output_dir / "selected_model_batches.jsonl", selected_models)
    write_jsonl(output_dir / "selected_oracle_batches.jsonl", selected_oracles)

    selected_ids = {row["batch_id"] for row in selected_models}
    response_by_id = {}
    resumable_ids = set()
    if resume:
        response_by_id, resumable_ids = _load_resumable_responses(
            output_dir / "model_responses.jsonl",
            selected_models,
            expected_fingerprints=expected_fingerprints,
            reuse_received=resume_received,
        )
        attempts = [
            row
            for row in read_jsonl(output_dir / "api_attempts.jsonl")
            if (
                isinstance(row, dict)
                and row.get("batch_id") in selected_ids
                and row.get("request_fingerprint")
                == expected_fingerprints[row.get("batch_id")]
            )
        ]
    else:
        attempts = []
    skipped_batch_count = len(resumable_ids)
    started = time.perf_counter()
    persistence_lock = threading.Lock()

    def persist_attempt(row):
        with persistence_lock:
            attempts.append(row)
            write_jsonl(output_dir / "api_attempts.jsonl", attempts)

    def persist_response(batch_id, final_response):
        with persistence_lock:
            if final_response is not None:
                response_by_id[batch_id] = final_response
            write_jsonl(
                output_dir / "model_responses.jsonl",
                [
                    response_by_id[row["batch_id"]]
                    for row in selected_models
                    if row["batch_id"] in response_by_id
                ],
            )

    def invoke(index, batch_record):
        batch_id = batch_record["batch_id"]
        request_timeout, request_loop_count = timeout_for_batch(
            batch_record,
            timeout,
            complex_timeout=complex_timeout,
            complex_loop_threshold=complex_loop_threshold,
        )
        print(
            f"[block-state-api] start {index}/{len(selected_models)} {batch_id} "
            f"timeout={request_timeout:g}s loops={request_loop_count}",
            flush=True,
        )
        final_response, _ = call_one_batch(
            client=client,
            batch_record=batch_record,
            model=model,
            timeout=request_timeout,
            max_completion_tokens=max_completion_tokens,
            retries=retries,
            retry_invalid=retry_invalid,
            on_attempt=persist_attempt,
            reasoning_effort=reasoning_effort,
            verbosity=verbosity,
            temperature=temperature,
            generation_config=generation_configs[batch_id],
            expected_fingerprint=expected_fingerprints[batch_id],
        )
        persist_response(batch_id, final_response)
        print(
            f"[block-state-api] done {index}/{len(selected_models)} {batch_id} "
            f"received={final_response is not None}",
            flush=True,
        )
        return batch_id

    pending = [
        (index, row)
        for index, row in enumerate(selected_models, start=1)
        if row["batch_id"] not in resumable_ids
        and (stage_ids is None or row["batch_id"] in stage_ids)
    ]
    for index, row in enumerate(selected_models, start=1):
        if (
            row["batch_id"] in resumable_ids
            and (stage_ids is None or row["batch_id"] in stage_ids)
        ):
            print(
                f"[block-state-api] {index}/{len(selected_models)} "
                f"{row['batch_id']} (resume)",
                flush=True,
            )
    if concurrency == 1:
        for index, batch_record in pending:
            invoke(index, batch_record)
    else:
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = {
                executor.submit(invoke, index, batch_record): batch_record["batch_id"]
                for index, batch_record in pending
            }
            for future in as_completed(futures):
                future.result()

    responses = [
        response_by_id[row["batch_id"]]
        for row in selected_models
        if row["batch_id"] in response_by_id
    ]
    write_jsonl(output_dir / "model_responses.jsonl", responses)

    if stage_ids is None:
        evaluation_models = selected_models
        evaluation_oracles = selected_oracles
    else:
        evaluation_models = [
            row for row in selected_models if row["batch_id"] in stage_ids
        ]
        evaluation_oracles = [
            row for row in selected_oracles if row["batch_id"] in stage_ids
        ]
    evaluation_ids = {row["batch_id"] for row in evaluation_models}
    evaluation_responses = [
        row for row in responses if row["batch_id"] in evaluation_ids
    ]
    evaluation = evaluate_response_records(
        evaluation_models,
        evaluation_oracles,
        evaluation_responses,
        output_dir / "evaluation",
    )
    received_attempts = [row for row in attempts if row.get("status") == "received"]
    received_latencies = [row["elapsed_seconds"] for row in received_attempts]
    summary = {
        "schema_version": API_RUN_SCHEMA_VERSION,
        "run_config_fingerprint": run_config["run_config_fingerprint"],
        "model": model,
        "api_base_url": api_base_url.rstrip("/") if api_base_url else None,
        "reasoning_effort": reasoning_effort,
        "verbosity": verbosity,
        "temperature": temperature,
        "max_completion_tokens": max_completion_tokens,
        "timeout_seconds": timeout,
        "complex_timeout_seconds": complex_timeout,
        "complex_loop_threshold": complex_loop_threshold,
        "concurrency": concurrency,
        "tasks": list(task_ids),
        "selected_task_count": len({task_id_from_batch(row["batch_id"]) for row in selected_models}),
        "selected_batch_count": len(selected_models),
        "selected_case_count": sum(len(row["request"]["cases"]) for row in selected_models),
        "invocation_scope": "stage" if stage_ids is not None else "full_plan",
        "invocation_selected_batch_count": len(evaluation_models),
        "invocation_selected_case_count": sum(
            len(row["request"]["cases"]) for row in evaluation_models
        ),
        "invocation_stage_case_keys_sha256": (
            _stable_hash([row["batch_id"] for row in evaluation_models])
            if stage_ids is not None
            else None
        ),
        "invocation_api_call_count": len(pending),
        "request_mode": "per_case" if one_case_per_request or flat_runs else "task_batch",
        "answer_format": "flat_runs" if flat_runs else "expanded",
        "one_case_per_request": one_case_per_request or flat_runs,
        "resume": resume,
        "resume_received": resume_received,
        "skipped_batch_count": skipped_batch_count,
        "response_count": len(responses),
        "api_attempt_count": len(attempts),
        "api_error_count": sum(row["status"] == "api_error" for row in attempts),
        "valid_attempt_count": sum(row.get("validation") == "valid" for row in attempts),
        "invalid_attempt_count": sum(row.get("validation") == "invalid" for row in attempts),
        "prompt_tokens": sum(row.get("prompt_tokens", 0) for row in attempts),
        "completion_tokens": sum(row.get("completion_tokens", 0) for row in attempts),
        "total_tokens": sum(row.get("total_tokens", 0) for row in attempts),
        "reasoning_tokens": sum(row.get("reasoning_tokens", 0) for row in attempts),
        "cached_prompt_tokens": sum(
            row.get("cached_prompt_tokens", 0) for row in attempts
        ),
        "received_latency_seconds": {
            "p50": _percentile(received_latencies, 0.50),
            "p90": _percentile(received_latencies, 0.90),
            "p95": _percentile(received_latencies, 0.95),
            "max": max(received_latencies) if received_latencies else None,
        },
        "elapsed_seconds": time.perf_counter() - started,
        "evaluation": evaluation["summary"],
    }
    write_json(output_dir / "summary.json", summary)
    return summary


def main():
    parser = argparse.ArgumentParser(
        description="Run selected block/state batches through an OpenAI-compatible Chat Completions API."
    )
    parser.add_argument("--model-batches", required=True)
    parser.add_argument("--oracle-batches", required=True)
    parser.add_argument("--output-dir", required=True)
    task_group = parser.add_mutually_exclusive_group(required=True)
    task_group.add_argument(
        "--tasks",
        help="Comma-separated task ids, for example task_109,task_126",
    )
    task_group.add_argument(
        "--all-tasks",
        action="store_true",
        help="Fix the run plan to every task present in --model-batches.",
    )
    parser.add_argument("--model", default=os.getenv("YUNWU_MODEL", ""))
    parser.add_argument(
        "--base-url",
        default=os.getenv("YUNWU_API_BASE_URL", DEFAULT_API_BASE_URL),
        help=f"OpenAI-compatible API base URL (default: {DEFAULT_API_BASE_URL}).",
    )
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument(
        "--complex-timeout",
        type=float,
        help="Use this timeout when a request has at least --complex-loop-threshold loop headers.",
    )
    parser.add_argument("--complex-loop-threshold", type=int, default=2)
    parser.add_argument("--max-completion-tokens", type=int, default=16000)
    parser.add_argument(
        "--reasoning-effort",
        choices=("none", "low", "medium", "high", "xhigh"),
        help="Explicit GPT-5 reasoning effort passed to Chat Completions.",
    )
    parser.add_argument(
        "--verbosity",
        choices=("low", "medium", "high"),
        help="Explicit GPT-5 output verbosity passed to Chat Completions.",
    )
    parser.add_argument("--temperature", type=float)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--retries", type=int, default=1)
    parser.add_argument(
        "--retry-invalid",
        action="store_true",
        help="Explicitly allow a paid retry when a received response fails schema validation.",
    )
    parser.add_argument("--transport", choices=("sdk", "http"), default="sdk")
    parser.add_argument("--max-cases-per-batch", type=int)
    parser.add_argument(
        "--one-case-per-request",
        action="store_true",
        help="Send each task input as its own API request, even if source batches contain multiple cases.",
    )
    parser.add_argument(
        "--flat-runs",
        action="store_true",
        help="Use one request per input and return canonical flat [path, repeat_count] block runs.",
    )
    parser.add_argument(
        "--max-requests",
        type=int,
        help="Limit the selected per-case requests for a paid canary run.",
    )
    parser.add_argument(
        "--case-keys-file",
        help="Optional UTF-8 file containing one task_id/input_id per line in canary order.",
    )
    parser.add_argument(
        "--stage-case-keys-file",
        help=(
            "Limit calls and interim evaluation to these case keys without changing the "
            "fixed full-run selection or resume fingerprint."
        ),
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Reuse only persisted responses that pass exact schema validation; retry missing/invalid cases.",
    )
    parser.add_argument(
        "--resume-received",
        action="store_true",
        help="With --resume, also preserve schema-invalid received responses and call only missing cases.",
    )
    args = parser.parse_args()

    if args.resume_received and not args.resume:
        raise SystemExit("--resume-received requires --resume")
    if args.reasoning_effort not in (None, "none") and args.temperature is not None:
        raise SystemExit(
            "GPT-5.4 temperature is only supported with reasoning_effort=none; "
            "omit --temperature for higher reasoning effort"
        )

    api_key = os.getenv("YUNWU_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("missing YUNWU_API_KEY environment variable")
    if not args.model.strip():
        raise SystemExit("missing YUNWU_MODEL environment variable or --model")
    if not args.base_url.strip():
        raise SystemExit("missing YUNWU_API_BASE_URL environment variable or --base-url")

    if args.transport == "http":
        client = create_http_compatible_client(api_key, args.base_url)
    else:
        client = create_openai_compatible_client(api_key, args.base_url)
    model_batches = read_jsonl(args.model_batches)
    oracle_batches = read_jsonl(args.oracle_batches)
    if args.all_tasks:
        task_ids = list(dict.fromkeys(
            task_id_from_batch(row["batch_id"]) for row in model_batches
        ))
    else:
        task_ids = [item.strip() for item in args.tasks.split(",") if item.strip()]
    case_keys = None
    if args.case_keys_file:
        case_keys = [
            line.strip()
            for line in Path(args.case_keys_file).read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        if not case_keys:
            raise SystemExit("--case-keys-file contains no case keys")
    stage_case_keys = None
    if args.stage_case_keys_file:
        stage_case_keys = [
            line.strip()
            for line in Path(args.stage_case_keys_file).read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        if not stage_case_keys:
            raise SystemExit("--stage-case-keys-file contains no case keys")
    summary = run_api_experiment(
        client=client,
        model_batches=model_batches,
        oracle_batches=oracle_batches,
        task_ids=task_ids,
        model=args.model,
        output_dir=args.output_dir,
        timeout=args.timeout,
        max_completion_tokens=args.max_completion_tokens,
        retries=args.retries,
        max_cases_per_batch=args.max_cases_per_batch,
        retry_invalid=args.retry_invalid,
        one_case_per_request=args.one_case_per_request,
        flat_runs=args.flat_runs,
        max_requests=args.max_requests,
        resume=args.resume,
        resume_received=args.resume_received,
        api_base_url=args.base_url,
        reasoning_effort=args.reasoning_effort,
        verbosity=args.verbosity,
        temperature=args.temperature,
        concurrency=args.concurrency,
        complex_timeout=args.complex_timeout,
        complex_loop_threshold=args.complex_loop_threshold,
        case_keys=case_keys,
        stage_case_keys=stage_case_keys,
    )
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
