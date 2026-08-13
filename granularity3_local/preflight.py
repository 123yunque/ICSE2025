import argparse
import ast
import json
import re
from pathlib import Path

from granularity3_local.oracle import choose_function, write_json


UNSUPPORTED_STATUS_ORDER = (
    "unsupported_jump",
    "unsupported_recursion",
    "unsupported_generator",
    "unsupported_loop_else",
    "unsupported_exception",
    "unsupported_context_manager",
    "unsupported_async",
)


def resolve_task_function(task_dir, requested=None):
    task_dir = Path(task_dir)
    source = (task_dir / "code.py").read_text(encoding="utf-8")
    if requested:
        return choose_function(source, requested)
    metadata_path = task_dir / f"{task_dir.name}.json"
    if metadata_path.exists():
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        for assertion in metadata.get("test_list") or []:
            match = re.search(r"\b([A-Za-z_]\w*)\s*\(", assertion)
            if match:
                name = match.group(1)
                try:
                    return choose_function(source, name)
                except ValueError:
                    pass
    return choose_function(source)


def preflight_source(source, function_name):
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return {"status": "invalid_syntax", "supported": False, "reasons": [str(exc)], "warnings": []}
    functions = {
        node.name: node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    target = functions.get(function_name)
    if target is None:
        return {
            "status": "missing_function",
            "supported": False,
            "reasons": [f"function not found: {function_name}"],
            "warnings": [],
        }

    statuses = set()
    reasons = []
    warnings = []
    if isinstance(target, ast.AsyncFunctionDef):
        statuses.add("unsupported_async")
        reasons.append("async target functions are not supported")
    if len(functions) > 1:
        warnings.append("helper_functions_are_treated_as_atomic_calls")

    for node in ast.walk(target):
        if isinstance(node, (ast.Break, ast.Continue)):
            statuses.add("unsupported_jump")
        if isinstance(node, (ast.For, ast.While, ast.AsyncFor)) and node.orelse:
            statuses.add("unsupported_loop_else")
        if isinstance(node, (ast.Yield, ast.YieldFrom)):
            statuses.add("unsupported_generator")
        if isinstance(node, (ast.Try, ast.Raise)):
            statuses.add("unsupported_exception")
        if isinstance(node, (ast.With, ast.AsyncWith)):
            statuses.add("unsupported_context_manager")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == function_name:
            statuses.add("unsupported_recursion")

    if "unsupported_jump" in statuses:
        reasons.append("target contains break or continue")
    if "unsupported_recursion" in statuses:
        reasons.append("target directly calls itself; frame-aware recursion is not implemented")
    if "unsupported_generator" in statuses:
        reasons.append("target contains yield/yield from")
    if "unsupported_loop_else" in statuses:
        reasons.append("target contains for-else or while-else")
    if "unsupported_exception" in statuses:
        reasons.append("target contains try or raise")
    if "unsupported_context_manager" in statuses:
        reasons.append("target contains with")

    status = next((item for item in UNSUPPORTED_STATUS_ORDER if item in statuses), "supported")
    return {
        "status": status,
        "supported": not statuses,
        "all_statuses": sorted(statuses),
        "reasons": reasons,
        "warnings": warnings,
        "function": function_name,
        "function_count": len(functions),
    }


def preflight_task(task_dir, requested_function=None):
    task_dir = Path(task_dir)
    source = (task_dir / "code.py").read_text(encoding="utf-8")
    try:
        function_name = resolve_task_function(task_dir, requested_function)
    except Exception as exc:
        return {
            "task_id": task_dir.name,
            "status": "missing_function",
            "supported": False,
            "reasons": [str(exc)],
            "warnings": [],
        }
    return {"task_id": task_dir.name, **preflight_source(source, function_name)}


def main():
    parser = argparse.ArgumentParser(description="Classify one task before local oracle execution.")
    parser.add_argument("--task-dir", required=True)
    parser.add_argument("--function")
    parser.add_argument("--output")
    args = parser.parse_args()
    result = preflight_task(args.task_dir, args.function)
    if args.output:
        write_json(args.output, result)
    print(json.dumps(result, ensure_ascii=False))
    if not result["supported"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()

