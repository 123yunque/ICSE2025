import unittest

from granularity3_local.legacy.joint_benchmark import aggregate


class JointBenchmarkTests(unittest.TestCase):
    def test_aggregate(self):
        score = {
            "line_trace_exact": True,
            "return_correct": True,
            "joint_exact": True,
            "joint_semantic_exact": True,
            "probe_count": 2,
            "next_correct_count": 2,
            "delta_correct_count": 1,
            "delta_semantic_correct_count": 2,
        }
        result = aggregate([{"status": "success", "score": score, "api": {"total_tokens": 10}}])
        self.assertEqual(result["next_accuracy"], 1)
        self.assertEqual(result["delta_accuracy"], 0.5)
        self.assertEqual(result["total_tokens"], 10)


if __name__ == "__main__":
    unittest.main()
