import argparse
import json
import os
import time
from pathlib import Path

def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def read_first_jsonl(path):
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            return json.loads(line)
    raise ValueError(f"empty JSONL: {path}")


def parse_json_response(text):
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    return json.loads(stripped)


def build_prompt(model_case, model_input):
    payload = {
        "task": model_case["task_definition"],
        "function": model_case["function"],
        "function_input": model_case["input"],
        "blocks": model_case["blocks"],
        "cfg_edges": model_case["cfg_edges"],
        "probe": model_input,
        "output_rules": [
            "Return one JSON object only.",
            "Predict next and delta.",
            "Include return only for a return event.",
            "Do not include explanations or markdown fences.",
        ],
    }
    return "Predict this concrete Python basic-block execution event:\n" + json.dumps(
        payload, ensure_ascii=False, sort_keys=True
    )


def compare_prediction(prediction, answer):
    fields = ["next", "delta"] + (["return"] if "return" in answer else [])
    comparison = {field: prediction.get(field) == answer.get(field) for field in fields}
    return {"fields": comparison, "exact": all(comparison.values())}


def exception_chain(exc):
    chain = []
    current = exc
    while current is not None and len(chain) < 5:
        chain.append({"type": type(current).__name__, "reason": str(current)})
        current = current.__cause__ or current.__context__
    return chain


def main():
    parser = argparse.ArgumentParser(description="Make one real API call using one generated granularity-3 probe.")
    parser.add_argument("--probe-dir", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--model", default=os.getenv("YUNWU_MODEL", ""))
    parser.add_argument("--base-url", default=os.getenv("YUNWU_API_BASE_URL", ""))
    parser.add_argument("--timeout", type=float, default=60)
    args = parser.parse_args()

    api_key = os.getenv("YUNWU_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("missing YUNWU_API_KEY environment variable")
    if not args.model.strip():
        raise SystemExit("missing YUNWU_MODEL environment variable or --model")
    if not args.base_url.strip():
        raise SystemExit("missing YUNWU_API_BASE_URL environment variable or --base-url")

    from openai import OpenAI

    probe_dir = Path(args.probe_dir)
    model_case = read_json(probe_dir / "model_case.json")
    model_input = read_first_jsonl(probe_dir / "model_inputs.jsonl")
    answer = read_first_jsonl(probe_dir / "answers.jsonl")
    prompt = build_prompt(model_case, model_input)
    client = OpenAI(
        api_key=api_key,
        base_url=args.base_url,
        max_retries=0,
    )
    started = time.perf_counter()
    try:
        response = client.chat.completions.create(
            model=args.model,
            messages=[
                {"role": "system", "content": "Return one valid JSON object only. No explanation."},
                {"role": "user", "content": prompt},
            ],
            temperature=0,
            timeout=args.timeout,
        )
        elapsed = time.perf_counter() - started
        content = response.choices[0].message.content or ""
        try:
            prediction = parse_json_response(content)
            parse_status = "success"
            comparison = compare_prediction(prediction, answer)
        except Exception as exc:
            prediction = None
            parse_status = "failed"
            comparison = None
            parse_error = f"{type(exc).__name__}: {exc}"
        usage = getattr(response, "usage", None)
        report = {
            "status": "success",
            "requested_model": args.model,
            "response_model": getattr(response, "model", None),
            "endpoint_configured": bool(args.base_url),
            "elapsed_seconds": elapsed,
            "finish_reason": response.choices[0].finish_reason,
            "probe_id": model_input["probe_id"],
            "response_content": content,
            "json_parse_status": parse_status,
            "prediction": prediction,
            "local_answer": {key: value for key, value in answer.items() if key not in {"schema_version", "probe_id"}},
            "comparison": comparison,
            "usage": {
                "prompt_tokens": getattr(usage, "prompt_tokens", None) if usage else None,
                "completion_tokens": getattr(usage, "completion_tokens", None) if usage else None,
                "total_tokens": getattr(usage, "total_tokens", None) if usage else None,
            },
        }
        if parse_status == "failed":
            report["parse_error"] = parse_error
    except Exception as exc:
        report = {
            "status": "failed",
            "requested_model": args.model,
            "endpoint_configured": bool(args.base_url),
            "elapsed_seconds": time.perf_counter() - started,
            "error_type": type(exc).__name__,
            "reason": str(exc),
            "exception_chain": exception_chain(exc),
        }
    finally:
        client.close()

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    if report["status"] != "success":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
