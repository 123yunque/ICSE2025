import math
import unittest

from granularity3_local.state import canonicalize, snapshot, state_delta


class StateTests(unittest.TestCase):
    def test_container_order_is_deterministic(self):
        left = canonicalize({"b": {3, 1, 2}, "a": [2, 1]})
        right = canonicalize({"a": [2, 1], "b": {2, 3, 1}})
        self.assertEqual(left, right)

    def test_float_special_values(self):
        self.assertEqual(canonicalize(float("inf")), {"$float": "+inf"})
        self.assertEqual(canonicalize(float("-inf")), {"$float": "-inf"})
        self.assertEqual(canonicalize(float("nan")), {"$float": "nan"})
        self.assertEqual(canonicalize(0.5), {"$float": "0x1.0000000000000p-1"})

    def test_snapshot_is_not_changed_by_later_mutation(self):
        values = {"items": [1, 2]}
        frozen = snapshot(values)
        values["items"].append(3)
        self.assertEqual(frozen["items"]["items"], [1, 2])

    def test_delta_tracks_creation_change_and_deletion(self):
        before = {"same": 1, "changed": 2, "deleted": 3}
        after = {"same": 1, "changed": 4, "created": 5}
        delta = state_delta(before, after)
        self.assertEqual(set(delta), {"changed", "created", "deleted"})
        self.assertEqual(delta["created"]["before"], {"$undefined": True})
        self.assertEqual(delta["deleted"]["after"], {"$undefined": True})


if __name__ == "__main__":
    unittest.main()
