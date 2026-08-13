import unittest

from granularity3_local.executor import execute_and_verify


class ExecutionTests(unittest.TestCase):
    def test_if_events(self):
        source = """\
def f(x):
    if x > 0:
        return x + 1
    y = -x
    return y
"""
        positive = execute_and_verify(source, "f", (2,))
        negative = execute_and_verify(source, "f", (-3,))
        self.assertEqual([e["block_id"] for e in positive["events"]], ["B001", "B002"])
        self.assertEqual([e["block_id"] for e in negative["events"]], ["B001", "B003", "B004"])
        self.assertEqual(negative["events"][1]["state_delta"]["y"]["after"], 3)

    def test_for_occurrences_and_deltas(self):
        source = """\
def f(items):
    total = 0
    for item in items:
        total += item
    return total
"""
        run = execute_and_verify(source, "f", ([1, 2, 3],))
        headers = [e for e in run["events"] if e["block_id"] == "B002"]
        bodies = [e for e in run["events"] if e["block_id"] == "B003"]
        self.assertEqual([e["occurrence"] for e in headers], [1, 2, 3, 4])
        self.assertEqual([e["branch_value"] for e in headers], [True, True, True, False])
        self.assertEqual([e["state_delta"]["total"]["after"] for e in bodies], [1, 3, 6])
        self.assertEqual(run["events"][-1]["return_value"], 6)

    def test_while_occurrences_and_deltas(self):
        source = """\
def f(n):
    total = 0
    while n > 0:
        total += n
        n -= 1
    return total
"""
        run = execute_and_verify(source, "f", (3,))
        headers = [e for e in run["events"] if e["block_id"] == "B002"]
        bodies = [e for e in run["events"] if e["block_id"] == "B003"]
        self.assertEqual([e["branch_value"] for e in headers], [True, True, True, False])
        self.assertEqual([e["state_delta"]["n"]["after"] for e in bodies], [2, 1, 0])
        self.assertEqual([e["state_delta"]["total"]["after"] for e in bodies], [3, 5, 6])
        self.assertEqual(run["result"], 6)


if __name__ == "__main__":
    unittest.main()
