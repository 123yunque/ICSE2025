import ast
import copy
import sys

from granularity3_local.cfg import build_cfg_and_instrument
from granularity3_local.runtime import TraceRuntime
from granularity3_local.state import canonicalize


def _call_args(input_value):
    if isinstance(input_value, tuple):
        return tuple(copy.deepcopy(input_value))
    return (copy.deepcopy(input_value),)


def execute_plain(source, function_name, input_value, filename="<plain>"):
    return execute_plain_with_line_trace(source, function_name, input_value, filename)[0]


def execute_plain_with_line_trace(source, function_name, input_value, filename="<plain>"):
    namespace = {"__name__": "__g3_plain__"}
    exec(compile(source, filename, "exec"), namespace)
    function = namespace[function_name]
    target_code = function.__code__
    line_numbers = []

    def tracer(frame, event, arg):
        if frame.f_code is target_code and event == "line":
            line_numbers.append(frame.f_lineno)
        return tracer

    previous = sys.gettrace()
    sys.settrace(tracer)
    try:
        result = function(*_call_args(input_value))
    finally:
        sys.settrace(previous)
    first_line = target_code.co_firstlineno
    return result, {
        "source_lines": line_numbers,
        "function_relative_lines": [line - first_line + 1 for line in line_numbers],
        "function_definition_line": first_line,
    }


def _edge_lookup(edges):
    lookup = {}
    for edge in edges:
        lookup[(edge["from"], edge["to"])] = edge["edge_type"]
    return lookup


def finalize_events(events, edges):
    lookup = _edge_lookup(edges)
    result = []
    for index, event in enumerate(events):
        item = {"event_index": index + 1, **event}
        next_block = events[index + 1]["block_id"] if index + 1 < len(events) else None
        item["next_block"] = next_block
        item["edge_type"] = lookup.get((event["block_id"], next_block))
        if item["edge_type"] is None:
            item["edge_type"] = "return" if "return_value" in event else "exit"
        result.append(item)
    return result


def execute_instrumented(
    source,
    function_name,
    input_value,
    filename="<instrumented>",
    max_events=10000,
    max_trace_bytes=None,
):
    analysis = build_cfg_and_instrument(source, function_name, filename=filename)
    runtime = TraceRuntime(max_events=max_events, max_trace_bytes=max_trace_bytes)
    namespace = {"__name__": "__g3_instrumented__", "__trace__": runtime}
    exec(compile(analysis["instrumented_tree"], filename, "exec"), namespace)
    result = namespace[function_name](*_call_args(input_value))
    return {
        "entry_block": analysis["entry_block"],
        "blocks": analysis["blocks"],
        "edges": analysis["edges"],
        "events": finalize_events(runtime.events, analysis["edges"]),
        "result": canonicalize(result),
    }


def execute_and_verify(
    source,
    function_name,
    input_value,
    filename="<source>",
    max_events=10000,
    max_trace_bytes=None,
):
    plain, line_trace = execute_plain_with_line_trace(
        source, function_name, input_value, filename=f"{filename}:plain"
    )
    traced = execute_instrumented(
        source,
        function_name,
        input_value,
        filename=f"{filename}:instrumented",
        max_events=max_events,
        max_trace_bytes=max_trace_bytes,
    )
    plain_value = canonicalize(plain)
    if plain_value != traced["result"]:
        raise AssertionError(f"instrumentation changed result: plain={plain_value!r}, traced={traced['result']!r}")
    traced["plain_result"] = plain_value
    traced["line_trace"] = line_trace
    traced["semantics_preserved"] = True
    return traced
