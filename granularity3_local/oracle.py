import argparse
import ast
import hashlib
import json
import platform
from pathlib import Path

from granularity3_local.executor import execute_and_verify


SCHEMA_VERSION = "g3-local-oracle-v1"


def parse_input(text):
    return ast.literal_eval(text)


def choose_function(source, requested=None):
    functions = [node.name for node in ast.parse(source).body if isinstance(node, ast.FunctionDef)]
    if requested:
        if requested not in functions:
            raise ValueError(f"function not found: {requested}")
        return requested
    if not functions:
        raise ValueError("no top-level function found")
    return functions[-1]


def code_hash(source):
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def stable_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, payload):
    Path(path).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def write_jsonl(path, rows):
    with Path(path).open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def build_oracle_case(
    source,
    function_name,
    input_text,
    task_id,
    variant,
    input_id,
    output_dir,
    source_path=None,
    max_events=10000,
    max_trace_bytes=None,
):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    case_id = f"{task_id}/{variant}/{input_id}"
    run = execute_and_verify(
        source,
        function_name,
        parse_input(input_text),
        filename=str(source_path or task_id),
        max_events=max_events,
        max_trace_bytes=max_trace_bytes,
    )
    metadata = {
        "schema_version": SCHEMA_VERSION,
        "case_id": case_id,
        "task_id": task_id,
        "variant": variant,
        "input_id": input_id,
        "input": input_text,
        "function": function_name,
        "code_sha256": code_hash(source),
        "python_version": platform.python_version(),
    }
    events = [{**metadata, **event} for event in run["events"]]
    cfg = {
        **metadata,
        "entry_block": run["entry_block"],
        "blocks": run["blocks"],
        "edges": run["edges"],
    }
    case = {
        **metadata,
        "source_path": str(Path(source_path).resolve()) if source_path else None,
        "semantics_preserved": run["semantics_preserved"],
        "result": run["result"],
        "event_count": len(events),
        "line_event_count": len(run["line_trace"]["source_lines"]),
        "executed_blocks": [event["block_id"] for event in events],
    }

    line_trace = {
        **metadata,
        **run["line_trace"],
        "line_event_count": len(run["line_trace"]["source_lines"]),
    }

    cfg_path = output_dir / "cfg.json"
    event_path = output_dir / "events.jsonl"
    case_path = output_dir / "case.json"
    line_trace_path = output_dir / "line_trace.json"
    write_json(cfg_path, cfg)
    write_jsonl(event_path, events)
    write_json(case_path, case)
    write_json(line_trace_path, line_trace)
    hashes = {
        "cfg_sha256": stable_hash(cfg_path),
        "events_sha256": stable_hash(event_path),
        "case_sha256": stable_hash(case_path),
        "line_trace_sha256": stable_hash(line_trace_path),
    }
    write_json(output_dir / "hashes.json", hashes)
    return {"case": case, "cfg": cfg, "events": events, "line_trace": line_trace, "hashes": hashes}


def main():
    parser = argparse.ArgumentParser(description="Build a deterministic local granularity-3 oracle case.")
    parser.add_argument("--code", required=True)
    parser.add_argument("--input", required=True)
    parser.add_argument("--function")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--variant", default="original")
    parser.add_argument("--input-id", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--max-events", type=int, default=10000)
    parser.add_argument("--max-trace-bytes", type=int)
    args = parser.parse_args()

    code_path = Path(args.code)
    source = code_path.read_text(encoding="utf-8")
    function_name = choose_function(source, args.function)
    result = build_oracle_case(
        source=source,
        function_name=function_name,
        input_text=args.input,
        task_id=args.task_id,
        variant=args.variant,
        input_id=args.input_id,
        output_dir=args.output_dir,
        source_path=code_path,
        max_events=args.max_events,
        max_trace_bytes=args.max_trace_bytes,
    )
    print(json.dumps({"case": result["case"], "hashes": result["hashes"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
