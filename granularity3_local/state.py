import copy
import json
import math


UNDEFINED = {"$undefined": True}


def canonicalize(value, active=None):
    """Convert a Python value to a deterministic JSON-compatible value."""
    if active is None:
        active = set()
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if math.isnan(value):
            return {"$float": "nan"}
        if math.isinf(value):
            return {"$float": "+inf" if value > 0 else "-inf"}
        return {"$float": value.hex()}

    identity = id(value)
    if identity in active:
        return {"$cycle": True}
    active.add(identity)
    try:
        if isinstance(value, list):
            return {"$type": "list", "items": [canonicalize(item, active) for item in value]}
        if isinstance(value, tuple):
            return {"$type": "tuple", "items": [canonicalize(item, active) for item in value]}
        if isinstance(value, dict):
            items = [[canonicalize(key, active), canonicalize(item, active)] for key, item in value.items()]
            items.sort(key=lambda pair: canonical_json(pair[0]))
            return {"$type": "dict", "items": items}
        if isinstance(value, (set, frozenset)):
            items = [canonicalize(item, active) for item in value]
            items.sort(key=canonical_json)
            return {"$type": "set", "items": items}
        return {"$type": type(value).__qualname__, "$repr": repr(value)}
    finally:
        active.remove(identity)


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def snapshot(local_vars):
    """Freeze user-visible locals immediately so later mutations cannot alter history."""
    result = {}
    for name, value in sorted(local_vars.items()):
        if name.startswith("__g3_") or name == "__trace__":
            continue
        try:
            frozen = copy.deepcopy(value)
        except Exception:
            frozen = value
        result[name] = canonicalize(frozen)
    return result


def state_delta(before, after):
    changes = {}
    for name in sorted(set(before) | set(after)):
        old = before.get(name, UNDEFINED)
        new = after.get(name, UNDEFINED)
        if old != new:
            changes[name] = {"before": old, "after": new}
    return changes

