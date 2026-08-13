import unittest

from granularity3_local.cfg import build_cfg_and_instrument


class CFGTests(unittest.TestCase):
    def test_if_cfg(self):
        source = """\
def f(x):
    if x > 0:
        return 1
    y = -x
    return y
"""
        cfg = build_cfg_and_instrument(source, "f")
        self.assertEqual(list(cfg["blocks"]), ["B001", "B002", "B003", "B004"])
        edges = {(e["from"], e["to"], e["edge_type"]) for e in cfg["edges"]}
        self.assertIn(("B001", "B002", "branch_true"), edges)
        self.assertIn(("B001", "B003", "branch_false"), edges)
        self.assertIn(("B003", "B004", "fallthrough"), edges)
        self.assertIn(("B004", None, "return"), edges)

    def test_for_cfg(self):
        source = """\
def f(items):
    total = 0
    for item in items:
        total += item
    return total
"""
        cfg = build_cfg_and_instrument(source, "f")
        edges = {(e["from"], e["to"], e["edge_type"]) for e in cfg["edges"]}
        self.assertIn(("B001", "B002", "fallthrough"), edges)
        self.assertIn(("B002", "B003", "loop_body"), edges)
        self.assertIn(("B003", "B002", "backedge"), edges)
        self.assertIn(("B002", "B004", "loop_exit"), edges)

    def test_while_cfg(self):
        source = """\
def f(n):
    total = 0
    while n > 0:
        total += n
        n -= 1
    return total
"""
        cfg = build_cfg_and_instrument(source, "f")
        edges = {(e["from"], e["to"], e["edge_type"]) for e in cfg["edges"]}
        self.assertIn(("B002", "B003", "loop_body"), edges)
        self.assertIn(("B003", "B002", "backedge"), edges)
        self.assertIn(("B002", "B004", "loop_exit"), edges)


if __name__ == "__main__":
    unittest.main()
