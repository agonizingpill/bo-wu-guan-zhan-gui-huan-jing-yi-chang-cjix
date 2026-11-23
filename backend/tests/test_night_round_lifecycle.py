"""用内存存储模拟 plan/rerun/depart 的业务语义，验证并发与幂等口径。

不依赖 Postgres：复刻 night_round_store 的关键规则
（部分唯一索引、已出发保留、被换下不可确认、重跑不新增同柜单）。
"""

import unittest

from src.night_round_ranking import ROUTINE_SLOT_LIMIT, rank_routine


class Conflict(Exception):
    pass


class FakeNightRoundStore:
    """最小化的 night_round_tasks 内存实现，语义对齐 SQL 约束与 store 流程。"""

    def __init__(self):
        self.rows = []
        self._id = 0

    def _routine_rows(self, shift_start):
        return [r for r in self.rows
                if r['shift_start'] == shift_start and r['round_kind'] == 'routine']

    def _active_key_exists(self, shift_start, case_id, kind):
        return any(
            r['shift_start'] == shift_start and r['case_id'] == case_id
            and r['round_kind'] == kind and r['status'] in ('pending', 'departed')
            for r in self.rows
        )

    def insert(self, shift_start, case_id, kind, slot_no, basis):
        # 对齐部分唯一索引 uniq_night_round_active
        if self._active_key_exists(shift_start, case_id, kind):
            raise AssertionError(f'重复同柜在途单: {case_id}/{kind}')
        self._id += 1
        self.rows.append({
            'id': self._id, 'shift_start': shift_start, 'case_id': case_id,
            'round_kind': kind, 'status': 'pending', 'slot_no': slot_no, 'basis': basis,
        })
        return self.rows[-1]

    def plan(self, shift_start, candidates):
        existing = self._routine_rows(shift_start)
        departed = {r['case_id']: r for r in existing if r['status'] == 'departed'}
        pending = {r['case_id']: r for r in existing if r['status'] == 'pending'}
        open_candidates = [c for c in candidates if c['case_id'] not in departed]
        chosen, _ = rank_routine(open_candidates, limit=ROUTINE_SLOT_LIMIT - len(departed))
        taken = {r['slot_no'] for r in departed.values()}
        free_slots = [s for s in range(1, ROUTINE_SLOT_LIMIT + 1) if s not in taken]
        chosen_cases = set()
        for offset, row in enumerate(chosen):
            row['slot_no'] = free_slots[offset]
            chosen_cases.add(row['case_id'])
            if row['case_id'] in pending:
                task = pending[row['case_id']]
                task['slot_no'] = row['slot_no']
                task['basis'] = {'deviation': row['deviation'], 'slot_no': row['slot_no']}
            else:
                self.insert(shift_start, row['case_id'], 'routine', row['slot_no'],
                            {'deviation': row['deviation'], 'slot_no': row['slot_no']})
        for case_id, task in pending.items():
            if case_id not in chosen_cases:
                task['status'] = 'superseded'
        return chosen

    def depart(self, task_id):
        task = next(r for r in self.rows if r['id'] == task_id)
        if task['status'] == 'departed':
            return task
        if task['status'] == 'superseded':
            raise Conflict('被换下，不能确认出发')
        task['status'] = 'departed'
        return task

    def active_routine(self, shift_start):
        return [r for r in self._routine_rows(shift_start)
                if r['status'] in ('pending', 'departed')]


def cand(case_id, current, history):
    return {'case_id': case_id, 'code': f'MC-{case_id}', 'current_score': current,
            'history_scores': history, 'event_id': case_id * 10}


# 统一班前基线 [-0.10,-0.11,-0.09]（std=0.01），偏离仅由当班分决定，便于断言
HISTORY = [-0.10, -0.11, -0.09]


class LifecycleTests(unittest.TestCase):

    def setUp(self):
        self.store = FakeNightRoundStore()
        self.shift = '2026-11-23T18:00:00Z'
        # 偏离随 case_id 递减：柜1 偏离最大
        self.base = [
            cand(1, -0.40, HISTORY),
            cand(2, -0.30, HISTORY),
            cand(3, -0.20, HISTORY),
            cand(4, -0.15, HISTORY),
        ]

    def test_initial_plan_two_distinct_cases(self):
        chosen = self.store.plan(self.shift, self.base)
        self.assertEqual([c['case_id'] for c in chosen], [1, 2])
        active = self.store.active_routine(self.shift)
        self.assertEqual(len(active), 2)
        self.assertEqual({r['case_id'] for r in active}, {1, 2})
        self.assertEqual(sorted(r['slot_no'] for r in active), [1, 2])

    def test_rerun_is_idempotent_no_duplicate(self):
        self.store.plan(self.shift, self.base)
        first_ids = {r['id'] for r in self.store.active_routine(self.shift)}
        self.store.plan(self.shift, self.base)  # 重跑，数据未变
        active = self.store.active_routine(self.shift)
        self.assertEqual(len(active), 2)
        self.assertEqual({r['id'] for r in active}, first_ids)  # 无新增

    def test_new_score_reassigns_pending_and_keeps_slots_unique(self):
        self.store.plan(self.shift, self.base)
        # 柜 4 出现大偏离新分（-0.95），偏离超过柜 2，顶替柜 2；柜 1 保持第一
        changed = [
            cand(1, -0.40, HISTORY),
            cand(2, -0.30, HISTORY),
            cand(3, -0.20, HISTORY),
            cand(4, -0.95, HISTORY),
        ]
        chosen = self.store.plan(self.shift, changed)
        self.assertEqual([c['case_id'] for c in chosen], [4, 1])
        active = self.store.active_routine(self.shift)
        self.assertEqual({r['case_id'] for r in active}, {1, 4})
        self.assertEqual(len({r['slot_no'] for r in active}), 2)
        case2 = next(r for r in self.store.rows if r['case_id'] == 2)
        self.assertEqual(case2['status'], 'superseded')

    def test_departed_task_is_kept_and_locks_its_slot_on_replan(self):
        self.store.plan(self.shift, self.base)
        task1 = next(r for r in self.store.rows if r['case_id'] == 1)
        self.store.depart(task1['id'])  # 柜 1 已出发
        changed = [
            cand(1, -0.40, HISTORY),
            cand(2, -0.30, HISTORY),
            cand(4, -0.95, HISTORY),
        ]
        self.store.plan(self.shift, changed)
        active = self.store.active_routine(self.shift)
        # 已出发的柜 1 保留，补一席给偏离最大的柜 4，仍不超过 2 席
        self.assertEqual({r['case_id'] for r in active}, {1, 4})
        self.assertEqual(len(active), ROUTINE_SLOT_LIMIT)
        self.assertEqual(sorted(r['slot_no'] for r in active), [1, 2])
        self.assertEqual(next(r for r in active if r['case_id'] == 1)['status'], 'departed')

    def test_superseded_task_cannot_depart(self):
        self.store.plan(self.shift, self.base)
        task2 = next(r for r in self.store.rows if r['case_id'] == 2)
        changed = [cand(1, -0.40, HISTORY), cand(4, -0.95, HISTORY)]
        self.store.plan(self.shift, changed)  # 柜 2 被换下
        with self.assertRaises(Conflict):
            self.store.depart(task2['id'])
        self.assertEqual(task2['status'], 'superseded')

    def test_depart_is_idempotent(self):
        self.store.plan(self.shift, self.base)
        task1 = next(r for r in self.store.rows if r['case_id'] == 1)
        self.store.depart(task1['id'])
        again = self.store.depart(task1['id'])  # 并发重复确认
        self.assertEqual(again['status'], 'departed')

    def test_case_without_history_never_gets_routine_slot(self):
        data = self.base + [cand(5, -0.99, [])]  # 极端原始分但无班前历史
        chosen = self.store.plan(self.shift, data)
        self.assertNotIn(5, [c['case_id'] for c in chosen])
        self.assertEqual(len(self.store.active_routine(self.shift)), 2)


if __name__ == '__main__':
    unittest.main()
