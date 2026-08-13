import argparse
import json
import os
import time
from pathlib import Path

from granularity3_local.api_smoke import exception_chain, parse_json_response


SYSTEM_PROMPT = """Simulate the target Python basic blocks. Return one JSON array only, in probe order. No explanation.
Each item must contain id, next, and delta. Include ret only for a return block.
delta contains only locals created, changed, or deleted by the current block, each as {\"before\":...,\"after\":...}.
An undefined value must be exactly {\"$u\":1}; never use null, undefined, unbound, or a string for it."""


def build_compact_prompt(batch):
    visible = {key: value for key, value in batch.items() if key not in {"schema", "batch_id"}}
    return json.dumps(visible, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def compare_batch(predictions, answer_batch):
    expected = answer_batch["answers"]
    predicted_by_id = {item.get("id"): item for item in predictions if isinstance(item, dict)}
    rows = []
    for answer in expected:
        prediction = predicted_by_id.get(answer["id"], {})
        fields = ["next", "delta"] + (["ret"] if "ret" in answer else [])
        comparisons = {field: prediction.get(field) == answer.get(field) for field in fields}
        rows.append({"id": answer["id"], "fields": comparisons, "exact": all(comparisons.values())})
    return {
        "probe_count": len(expected),
        "exact_count": sum(row["exact"] for row in rows),
        "rows": rows,
    }


def main():
    parser = argparse.ArgumentParser(description="Call one compact granularity-3 probe batch.")
    parser.add_argument("--batch-dir", required=True)
    parser.add_argument("--batch-index", type=int, default=1)
    parser.add_argument("--output", required=True)
    parser.add_argument("--model", default=os.getenv("YUNWU_MODEL", ""))
    parser.add_argument("--base-url", default=os.getenv("YUNWU_API_BASE_URL", ""))
    parser.add_argument("--timeout", type=float, default=60)
    args = parser.parse_args()
    api_key = os.getenv("YUNWU_API_KEY", "").strip()
    if not api_key or not args.model.strip() or not args.base_url.strip():
        raise SystemExit("YUNWU_API_KEY, YUNWU_MODEL, and YUNWU_API_BASE_URL are required")

    def read_lines(name):
        return [json.loads(line) for line in (Path(args.batch_dir) / name).read_text(encoding="utf-8").splitlines() if line.strip()]

    batches = read_lines("model_batches.jsonl")
    answers = read_lines("answer_batches.jsonl")
    index = args.batch_index - 1
    batch = batches[index]
    answer = answers[index]
    prompt = build_compact_prompt(batch)
    from openai import OpenAI
    client = OpenAI(api_key=api_key, base_url=args.base_url, max_retries=0)
    started = time.perf_counter()
    try:
        response = client.chat.completions.create(
            model=args.model,
            messages=[{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}],
            temperature=0,
            timeout=args.timeout,
        )
        content = response.choices[0].message.content or ""
        predictions = parse_json_response(content)
        if not isinstance(predictions, list):
            raise ValueError("model response must be a JSON array")
        usage = response.usage
        report = {
            "status": "success",
            "batch_id": batch["batch_id"],
            "model": getattr(response, "model", args.model),
            "elapsed_seconds": time.perf_counter() - started,
            "response_content": content,
            "predictions": predictions,
            "comparison": compare_batch(predictions, answer),
            "usage": {
                "prompt_tokens": getattr(usage, "prompt_tokens", None),
                "completion_tokens": getattr(usage, "completion_tokens", None),
                "total_tokens": getattr(usage, "total_tokens", None),
            },
        }
    except Exception as exc:
        report = {
            "status": "failed",
            "batch_id": batch["batch_id"],
            "elapsed_seconds": time.perf_counter() - started,
            "error_type": type(exc).__name__,
            "reason": str(exc),
            "exception_chain": exception_chain(exc),
        }
    finally:
        client.close()
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    if report["status"] != "success":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
