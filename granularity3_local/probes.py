import argparse
import json
from pathlib import Path

from granularity3_local.oracle import write_json, write_jsonl


PROBE_SCHEMA_VERSION = "g3-local-probe-v1"


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def read_jsonl(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def successors(cfg, block_id):
    return [
        {"block": edge["to"], "edge_type": edge["edge_type"]}
        for edge in cfg["edges"]
        if edge["from"] == block_id
    ]


def build_probe_dataset(oracle_dir, output_dir):
    oracle_dir = Path(oracle_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    case = read_json(oracle_dir / "case.json")
    cfg = read_json(oracle_dir / "cfg.json")
    events = read_jsonl(oracle_dir / "events.jsonl")

    model_case = {
        "schema_version": PROBE_SCHEMA_VERSION,
        "case_id": case["case_id"],
        "task_id": case["task_id"],
        "variant": case["variant"],
        "function": case["function"],
        "input": case["input"],
        "entry_block": cfg["entry_block"],
        "blocks": cfg["blocks"],
        "cfg_edges": cfg["edges"],
        "task_definition": {
            "next": "The direct basic block entered immediately after the target event; null on return.",
            "delta": "Only local variables created, changed, or deleted by the target block, as before/after values.",
            "return": "The returned value; include only when next is null because the target block returns.",
        },
    }

    model_inputs = []
    answers = []
    for event in events:
        probe_id = f"{case['case_id']}/{event['event_id']}"
        model_inputs.append({
            "schema_version": PROBE_SCHEMA_VERSION,
            "probe_id": probe_id,
            "case_id": case["case_id"],
            "target_event": event["event_id"].split("/", 1)[1],
            "current_block": event["block_id"],
            "current_source": cfg["blocks"][event["block_id"]]["source"],
            "state_before": event["state_before"],
            "allowed_successors": successors(cfg, event["block_id"]),
            "required_output": {"next": "block ID or null", "delta": "object", "return": "optional value"},
        })
        answer = {
            "schema_version": PROBE_SCHEMA_VERSION,
            "probe_id": probe_id,
            "next": event["next_block"],
            "delta": event["state_delta"],
        }
        if "return_value" in event:
            answer["return"] = event["return_value"]
        answers.append(answer)

    write_json(output_dir / "model_case.json", model_case)
    write_jsonl(output_dir / "model_inputs.jsonl", model_inputs)
    write_jsonl(output_dir / "answers.jsonl", answers)
    manifest = {
        "schema_version": PROBE_SCHEMA_VERSION,
        "case_id": case["case_id"],
        "probe_count": len(model_inputs),
        "model_case": "model_case.json",
        "model_inputs": "model_inputs.jsonl",
        "local_answers": "answers.jsonl",
        "answer_isolation": True,
    }
    write_json(output_dir / "manifest.json", manifest)
    return {"case": model_case, "model_inputs": model_inputs, "answers": answers, "manifest": manifest}


def main():
    parser = argparse.ArgumentParser(description="Export model-visible probes and isolated local answers.")
    parser.add_argument("--oracle-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    result = build_probe_dataset(args.oracle_dir, args.output_dir)
    print(json.dumps(result["manifest"], ensure_ascii=False))


if __name__ == "__main__":
    main()

