"""不依赖数据库的 store 辅助函数测试：用桩件替换 psycopg2 与连接层。"""

import sys
import types
import unittest
from datetime import datetime, timezone
from decimal import Decimal

# 桩件必须在导入 night_round_store 之前就位
_psycopg2 = types.ModuleType('psycopg2')
_extras = types.ModuleType('psycopg2.extras')
_extras.RealDictCursor = object
_psycopg2.extras = _extras
_psycopg2.connect = lambda *a, **k: None
sys.modules.setdefault('psycopg2', _psycopg2)
sys.modules.setdefault('psycopg2.extras', _extras)
_stub_db = types.ModuleType('src.utils.db')
_stub_db.get_conn = lambda: None
sys.modules.setdefault('src.utils.db', _stub_db)

from src.night_round_store import (  # noqa: E402
    _expedited_basis,
    _routine_basis,
    default_shift_window,
    window_for,
)


class WindowTests(unittest.TestCase):

    def test_window_for_is_13_hours_and_truncates_minutes(self):
        start, end = window_for(datetime(2026, 11, 23, 18, 35, tzinfo=timezone.utc))
        self.assertEqual(start, datetime(2026, 11, 23, 18, 0, tzinfo=timezone.utc))
        self.assertEqual((end - start).total_seconds(), 13 * 3600)

    def test_default_window_night_time_uses_today_18(self):
        start, end = default_shift_window(datetime(2026, 11, 23, 22, 0, tzinfo=timezone.utc))
        self.assertEqual(start.day, 23)
        self.assertEqual(start.hour, 18)

    def test_default_window_before_7am_uses_previous_evening(self):
        start, _ = default_shift_window(datetime(2026, 11, 23, 3, 0, tzinfo=timezone.utc))
        self.assertEqual(start, datetime(2026, 11, 22, 18, 0, tzinfo=timezone.utc))


class BasisTests(unittest.TestCase):

    def test_routine_basis_records_selection_evidence(self):
        shift_start = datetime(2026, 11, 23, 18, 0, tzinfo=timezone.utc)
        shift_end = datetime(2026, 11, 24, 7, 0, tzinfo=timezone.utc)
        row = {
            'case_id': 3, 'code': 'MC-03', 'event_id': 12, 'event_at': shift_start,
            'current_score': -0.31, 'history_scores': [-0.1, -0.12, -0.11],
            'history_mean': -0.11, 'history_std': 0.01, 'signed_deviation': -20.0,
            'deviation': 20.0, 'deviation_method': 'z_score', 'slot_no': 1,
        }
        basis = _routine_basis(row, shift_start, shift_end)
        self.assertEqual(basis['slot_no'], 1)
        self.assertEqual(basis['history_count'], 3)
        self.assertIn('标准化偏离 20.0', basis['rank_reason'])
        self.assertIn('例行第 1 席', basis['rank_reason'])

    def test_expedited_basis_flags_both_violations_and_door(self):
        row = {
            'sample_id': 9, 'case_id': 2, 'code': 'MC-02',
            'sampled_at': datetime(2026, 11, 23, 19, 0, tzinfo=timezone.utc),
            'temperature': Decimal('25.4'), 'humidity': Decimal('60.0'),
            'door_open': True,
            'target_temp_min': Decimal('18.0'), 'target_temp_max': Decimal('22.0'),
            'target_rh_min': Decimal('42.0'), 'target_rh_max': Decimal('52.0'),
        }
        basis = _expedited_basis(row)
        self.assertEqual(set(basis['violations']), {'temperature', 'humidity'})
        self.assertTrue(basis['door_open'])
        self.assertIn('加急处理，不占例行名额', basis['rank_reason'])
        self.assertIn('门磁开启段', basis['rank_reason'])

    def test_expedited_basis_in_range_collects_nothing(self):
        row = {
            'sample_id': 9, 'case_id': 2, 'code': 'MC-02',
            'sampled_at': datetime(2026, 11, 23, 19, 0, tzinfo=timezone.utc),
            'temperature': Decimal('20.0'), 'humidity': Decimal('48.0'), 'door_open': False,
            'target_temp_min': Decimal('18.0'), 'target_temp_max': Decimal('22.0'),
            'target_rh_min': Decimal('42.0'), 'target_rh_max': Decimal('52.0'),
        }
        self.assertEqual(_expedited_basis(row)['violations'], [])


if __name__ == '__main__':
    unittest.main()
