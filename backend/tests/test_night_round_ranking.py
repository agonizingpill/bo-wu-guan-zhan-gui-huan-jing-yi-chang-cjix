import unittest

from src.night_round_ranking import ROUTINE_SLOT_LIMIT, rank_routine, z_deviation


def candidate(case_id, current, history):
    return {'case_id': case_id, 'code': f'MC-{case_id:02d}',
            'current_score': current, 'history_scores': history}


class ZDeviationTests(unittest.TestCase):

    def test_standardized_deviation(self):
        result = z_deviation(-0.16, [-0.10, -0.11, -0.09])
        abs_dev, signed, mean, std, method = result
        self.assertAlmostEqual(mean, -0.10)
        self.assertAlmostEqual(std, 0.01)
        self.assertAlmostEqual(signed, -6.0)
        self.assertAlmostEqual(abs_dev, 6.0)
        self.assertEqual(method, 'z_score')

    def test_zero_variance_falls_back_to_mean_delta(self):
        result = z_deviation(-0.20, [-0.10, -0.10])
        abs_dev, signed, _mean, std, method = result
        self.assertEqual(std, 0.0)
        self.assertEqual(method, 'mean_delta')
        self.assertAlmostEqual(signed, -0.10)
        self.assertAlmostEqual(abs_dev, 0.10)

    def test_no_history_returns_none(self):
        self.assertIsNone(z_deviation(-0.20, []))


class RankRoutineTests(unittest.TestCase):

    def test_ranks_by_own_baseline_deviation_not_raw_score(self):
        # A 的原始分 -0.16 高于 B 的 -0.35（直接比会把 B 排前面），
        # 但按各自班前基线标准化后，A 的偏离(6)大于 B(5)。
        rows = [
            candidate(1, -0.16, [-0.10, -0.11, -0.09]),
            candidate(2, -0.35, [-0.30, -0.31, -0.29]),
        ]
        chosen, rest = rank_routine(rows, limit=2)
        self.assertEqual([row['case_id'] for row in chosen], [1, 2])
        self.assertGreater(chosen[0]['deviation'], chosen[1]['deviation'])

    def test_two_slots_and_one_per_case(self):
        rows = [candidate(i, -0.2 - i * 0.01, [-0.10, -0.12, -0.11]) for i in range(1, 5)]
        chosen, rest = rank_routine(rows)
        self.assertEqual(len(chosen), ROUTINE_SLOT_LIMIT)
        self.assertEqual(len({row['case_id'] for row in chosen}), len(chosen))
        self.assertEqual([row['slot_no'] for row in chosen], [1, 2])
        self.assertEqual(len(rest), 2)

    def test_cases_without_pre_shift_score_are_excluded(self):
        rows = [
            candidate(1, -0.16, [-0.10, -0.11, -0.09]),
            candidate(2, -0.99, []),
            candidate(3, -0.17, [-0.10, -0.11, -0.09]),
        ]
        chosen, rest = rank_routine(rows)
        self.assertEqual([row['case_id'] for row in chosen], [3, 1])
        excluded = [row for row in rest if row.get('exclude_reason') == 'no_pre_shift_score']
        self.assertEqual([row['case_id'] for row in excluded], [2])

    def test_tie_breaks_by_case_id(self):
        rows = [
            candidate(2, -0.15, [-0.10, -0.11, -0.09]),
            candidate(1, -0.15, [-0.10, -0.11, -0.09]),
        ]
        chosen, _ = rank_routine(rows)
        self.assertEqual([row['case_id'] for row in chosen], [1, 2])


if __name__ == '__main__':
    unittest.main()
