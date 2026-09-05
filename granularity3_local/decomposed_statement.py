"""Statement-level state tracing for decomposed variable-state Oracles.

The legacy runtime records one state delta per basic-block occurrence.  A basic
block may contain several assignments to the same variable, which is too coarse
for the decomposed protocol's "entry state + every actual change" definition.
This module adds an independent, semantics-preserving statement tracer without
changing the legacy block Oracle.
"""

from __future__ import annotations

import ast
import copy

from granularity3_local.state import canonicalize, snapshot, state_delta


class StatementEventLimitExceeded(RuntimeError):
    pass


class StatementResultMismatch(RuntimeError):
    """Instrumentation did not reproduce the trusted runtime result."""


class StatementStateRuntime:
    def __init__(self, max_events=10000):
        self.max_events = max_events
        self.last_state = None
        self.events = []

    def start(self, local_vars):
        self.last_state = snapshot(local_vars)

    def capture(self, statement_id, local_vars):
        current = snapshot(local_vars)
        if self.last_state is None:
            self.last_state = current
            return
        if self.max_events and len(self.events) >= self.max_events:
            raise StatementEventLimitExceeded(
                f"statement state event limit exceeded: {self.max_events}"
            )
        self.events.append({
            "statement_id": statement_id,
            "state_before": self.last_state,
            "state_after": current,
            "state_delta": state_delta(self.last_state, current),
        })
        self.last_state = current

    def predicate(self, value, statement_id, local_vars):
        self.capture(statement_id, local_vars)
        return value

    def return_value(self, value, statement_id, local_vars):
        self.capture(statement_id, local_vars)
        return value


class StatementStateInstrumenter:
    """Insert a snapshot hook after every target-function statement."""

    def __init__(self, function_name):
        self.function_name = function_name
        self.next_statement = 1

    @staticmethod
    def _locals_call():
        return ast.Call(ast.Name("locals", ast.Load()), [], [])

    @staticmethod
    def _runtime_method(name):
        return ast.Attribute(
            ast.Name("__g3_state_trace__", ast.Load()),
            name,
            ast.Load(),
        )

    def _identity(self, statement):
        statement_id = f"S{self.next_statement:04d}"
        self.next_statement += 1
        return statement_id, getattr(statement, "lineno", None)

    def _capture_expr(self, statement_id, location):
        expression = ast.Expr(ast.Call(
            self._runtime_method("capture"),
            [ast.Constant(statement_id), self._locals_call()],
            [],
        ))
        return ast.copy_location(expression, location)

    def _predicate_call(self, value, statement_id):
        return ast.Call(
            self._runtime_method("predicate"),
            [value, ast.Constant(statement_id), self._locals_call()],
            [],
        )

    def instrument_body(self, statements):
        output = []
        for statement in statements:
            output.extend(self.instrument_statement(statement))
        return output

    def instrument_statement(self, statement):
        statement_id, _line = self._identity(statement)

        if isinstance(statement, ast.If):
            statement.test = self._predicate_call(statement.test, statement_id)
            statement.body = self.instrument_body(statement.body)
            statement.orelse = self.instrument_body(statement.orelse)
            return [statement]

        if isinstance(statement, ast.While):
            statement.test = self._predicate_call(statement.test, statement_id)
            statement.body = self.instrument_body(statement.body)
            statement.orelse = self.instrument_body(statement.orelse)
            return [statement]

        if isinstance(statement, (ast.For, ast.AsyncFor)):
            target_capture = self._capture_expr(statement_id, statement)
            statement.body = [target_capture, *self.instrument_body(statement.body)]
            statement.orelse = self.instrument_body(statement.orelse)
            return [statement]

        if isinstance(statement, ast.Return):
            value = statement.value if statement.value is not None else ast.Constant(None)
            statement.value = ast.Call(
                self._runtime_method("return_value"),
                [value, ast.Constant(statement_id), self._locals_call()],
                [],
            )
            return [statement]

        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            # A nested definition only binds its name in the target frame.  Do
            # not instrument the nested frame itself.
            return [statement, self._capture_expr(statement_id, statement)]

        if isinstance(statement, (ast.Try, ast.With, ast.AsyncWith)) or (
            hasattr(ast, "Match") and isinstance(statement, ast.Match)
        ):
            raise ValueError(
                f"{type(statement).__name__} is not supported by statement-state tracing"
            )

        if isinstance(statement, (ast.Break, ast.Continue, ast.Raise)):
            return [statement]

        return [statement, self._capture_expr(statement_id, statement)]

    def transform(self, tree):
        target = next(
            (
                node
                for node in tree.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == self.function_name
            ),
            None,
        )
        if target is None:
            raise ValueError(f"function not found: {self.function_name}")
        if isinstance(target, ast.AsyncFunctionDef):
            raise ValueError("async target functions are not supported")
        start = ast.Expr(ast.Call(
            self._runtime_method("start"),
            [self._locals_call()],
            [],
        ))
        start = ast.copy_location(start, target.body[0])
        docstring = []
        body = target.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            docstring = [body[0]]
            body = body[1:]
        target.body = [*docstring, start, *self.instrument_body(body)]
        ast.fix_missing_locations(tree)
        return tree


def _call_args(input_value):
    if isinstance(input_value, tuple):
        return tuple(copy.deepcopy(input_value))
    return (copy.deepcopy(input_value),)


def execute_statement_state_trace(
    source,
    function_name,
    input_value,
    filename="<statement-state>",
    max_events=10000,
):
    tree = ast.parse(source, filename=filename)
    instrumented = StatementStateInstrumenter(function_name).transform(tree)
    runtime = StatementStateRuntime(max_events=max_events)
    namespace = {
        "__name__": "__g3_statement_state__",
        "__g3_state_trace__": runtime,
    }
    exec(compile(instrumented, filename, "exec"), namespace)
    result = namespace[function_name](*_call_args(input_value))
    return {
        "result": canonicalize(result),
        "events": runtime.events,
    }
