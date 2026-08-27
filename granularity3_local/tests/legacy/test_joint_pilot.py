import unittest

from granularity3_local.legacy.joint_pilot import edit_distance, longest_common_prefix, score_prediction


class JointPilotTests(unittest.TestCase):
    def test_sequence_metrics(self):
        self.assertEqual(longest_common_prefix([1, 2, 3], [1, 2, 4]), 2)
        self.assertEqual(edit_distance([1, 2, 3], [1, 4, 3]), 1)

    def test_joint_score(self):
        expected = {
            "line_trace": [2, 3],
            "probes": [{"id": "p1", "next_block": "B2", "state_delta": {}}],
            "return_value": 1,
        }
        prediction = {
            "line_trace": [2, 3],
            "probes": [{"id": "p1", "next_block": "B2", "state_delta": {}}],
            "return_value": 1,
        }
        score = score_prediction(prediction, expected)
        self.assertTrue(score["joint_exact"])
        self.assertTrue(score["joint_semantic_exact"])
        self.assertEqual(score["probe_exact_count"], 1)


if __name__ == "__main__":
    unittest.main()
