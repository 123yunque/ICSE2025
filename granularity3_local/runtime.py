from collections import defaultdict

from granularity3_local.state import canonicalize, snapshot, state_delta
from granularity3_local.state import canonical_json


class EventLimitExceeded(RuntimeError):
    pass


class TraceSizeLimitExceeded(RuntimeError):
    pass


class TraceRuntime:
    def __init__(self, max_events=10000, max_trace_bytes=None):
        self.max_events = max_events
        self.max_trace_bytes = max_trace_bytes
        self.trace_bytes = 0
        self.events = []
        self.occurrences = defaultdict(int)
        self.open_blocks = {}
        self.last_state = None

    def _identity(self, block_id):
        if self.max_events and len(self.events) >= self.max_events:
            raise EventLimitExceeded(f"dynamic event limit exceeded: {self.max_events}")
        self.occurrences[block_id] += 1
        occurrence = self.occurrences[block_id]
        return occurrence, f"F0/{block_id}#{occurrence}"

    def _append(self, block_id, before, after, **extra):
        occurrence, event_id = self._identity(block_id)
        event = {
            "event_id": event_id,
            "frame_id": "F0",
            "block_id": block_id,
            "occurrence": occurrence,
            "state_before": before,
            "state_after": after,
            "state_delta": state_delta(before, after),
            **extra,
        }
        event_bytes = len(canonical_json(event).encode("utf-8")) + 1
        if self.max_trace_bytes and self.trace_bytes + event_bytes > self.max_trace_bytes:
            raise TraceSizeLimitExceeded(
                f"dynamic trace size limit exceeded: {self.trace_bytes + event_bytes}>{self.max_trace_bytes}"
            )
        self.trace_bytes += event_bytes
        self.events.append(event)
        self.last_state = after
        return event

    def enter(self, block_id, local_vars):
        self.open_blocks[block_id] = snapshot(local_vars)

    def exit(self, block_id, local_vars):
        before = self.open_blocks.pop(block_id)
        after = snapshot(local_vars)
        self._append(block_id, before, after)

    def predicate(self, block_id, value, local_vars, predicate_kind):
        current = snapshot(local_vars)
        before = self.last_state if self.last_state is not None else current
        self._append(
            block_id,
            before,
            current,
            predicate_kind=predicate_kind,
            branch_value=bool(value),
        )
        return value

    def for_iteration(self, block_id, local_vars):
        current = snapshot(local_vars)
        before = self.last_state if self.last_state is not None else current
        self._append(
            block_id,
            before,
            current,
            predicate_kind="for",
            branch_value=True,
        )

    def for_exit(self, block_id, local_vars):
        current = snapshot(local_vars)
        before = self.last_state if self.last_state is not None else current
        self._append(
            block_id,
            before,
            current,
            predicate_kind="for",
            branch_value=False,
        )

    def return_value(self, block_id, value, local_vars):
        current = snapshot(local_vars)
        before = self.last_state if self.last_state is not None else current
        self._append(
            block_id,
            before,
            current,
            return_value=canonicalize(value),
        )
        return value
