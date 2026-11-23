"""夜班巡检安排的持久化与并发控制。

并发口径：
- 同一班次的例行排程通过 pg_advisory_xact_lock 串行化，重跑不产生重复同柜单；
- 出发确认对任务行 SELECT ... FOR UPDATE，与改派互斥：
  已出发的安排改派时保留，被换下(pending->superseded)的安排无法确认出发。
"""

import json
from datetime import datetime, timedelta, timezone
from threading import Lock

import psycopg2.extras

from .night_round_ranking import ROUTINE_SLOT_LIMIT, rank_routine
from .utils.db import get_conn

ADVISORY_KEY = 913711  # night-round 命名空间
_schema_lock = Lock()
_schema_ready = False

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS night_round_tasks (
    id SERIAL PRIMARY KEY,
    shift_start TIMESTAMPTZ NOT NULL,
    shift_end TIMESTAMPTZ NOT NULL,
    case_id INTEGER NOT NULL REFERENCES display_cases(id) ON DELETE CASCADE,
    round_kind VARCHAR(20) NOT NULL CHECK (round_kind IN ('routine','expedited')),
    status VARCHAR(20) NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','superseded','departed')),
    slot_no INTEGER,
    basis JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_by INTEGER REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    departed_at TIMESTAMPTZ,
    departed_by INTEGER REFERENCES users(id),
    CHECK ((round_kind='routine') = (slot_no IS NOT NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS uniq_night_round_active
    ON night_round_tasks(shift_start, case_id, round_kind)
    WHERE status IN ('pending','departed');
CREATE INDEX IF NOT EXISTS idx_night_round_shift
    ON night_round_tasks(shift_start, round_kind, status);
"""


def ensure_schema():
    global _schema_ready
    with _schema_lock:
        if _schema_ready:
            return
        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(SCHEMA_SQL)
        _schema_ready = True


def as_utc(value):
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def default_shift_window(now=None):
    """夜班窗口 18:00—次日 07:00（UTC 存储，展示层自行本地化）。"""
    now = as_utc(now or datetime.now(timezone.utc))
    today_18 = now.replace(hour=18, minute=0, second=0, microsecond=0)
    today_7 = now.replace(hour=7, minute=0, second=0, microsecond=0)
    if now >= today_18:
        start = today_18
    elif now < today_7:
        start = today_18 - timedelta(days=1)
    else:  # 白班间隙，默认回到刚结束的夜班窗口
        start = today_18 - timedelta(days=1)
    return start, start + timedelta(hours=13)


def window_for(shift_start):
    """把班次起点规整为整点并返回 13 小时窗口；仅接受夜班 18 点档，避免错档。"""
    start = as_utc(shift_start).replace(minute=0, second=0, microsecond=0)
    if start.hour != 18:
        raise ValueError('班次起点必须是 18:00 的夜班窗口')
    return start, start + timedelta(hours=13)


def _dicts(cur):
    return [dict(row) for row in cur.fetchall()]


def fetch_candidates(cur, shift_start, shift_end):
    """当班最新异常分 + 班前历史异常分（无班前数据的展柜仍返回，由排序层剔除）。"""
    cur.execute(
        """
        WITH latest_in_shift AS (
            SELECT DISTINCT ON (e.case_id)
                   e.case_id, e.id AS event_id, e.score AS current_score, e.created_at AS event_at
            FROM anomaly_events e
            WHERE e.created_at >= %s AND e.created_at < %s
            ORDER BY e.case_id, e.created_at DESC, e.id DESC
        )
        SELECT l.case_id, l.event_id, l.current_score, l.event_at,
               c.code, c.artifact_title, c.gallery,
               COALESCE((SELECT jsonb_agg(h.score ORDER BY h.created_at, h.id)
                         FROM anomaly_events h
                         WHERE h.case_id = l.case_id AND h.created_at < %s), '[]'::jsonb) AS history_scores
        FROM latest_in_shift l
        JOIN display_cases c ON c.id = l.case_id
        WHERE c.status <> 'retired'
        ORDER BY l.case_id
        """,
        (shift_start, shift_end, shift_start),
    )
    rows = _dicts(cur)
    for row in rows:
        row['history_scores'] = [float(score) for score in (row['history_scores'] or [])]
        row['current_score'] = float(row['current_score'])
    return rows


def fetch_violations(cur, shift_start, shift_end):
    """当班温湿越出展柜保护范围的最新采样（门开与否都加急，口径在采样约束本身）。"""
    cur.execute(
        """
        SELECT DISTINCT ON (s.case_id)
               s.id AS sample_id, s.case_id, c.code, c.artifact_title, c.gallery,
               s.sampled_at, s.temperature, s.humidity, s.door_open,
               c.target_temp_min, c.target_temp_max, c.target_rh_min, c.target_rh_max
        FROM environment_samples s
        JOIN display_cases c ON c.id = s.case_id
        WHERE s.state='accepted' AND s.sampled_at >= %s AND s.sampled_at < %s
          AND c.status <> 'retired'
          AND (s.temperature NOT BETWEEN c.target_temp_min AND c.target_temp_max
               OR s.humidity NOT BETWEEN c.target_rh_min AND c.target_rh_max)
        ORDER BY s.case_id, s.sampled_at DESC, s.id DESC
        """,
        (shift_start, shift_end),
    )
    return _dicts(cur)


def _routine_basis(row, shift_start, shift_end):
    return {
        'shift_start': shift_start.isoformat(),
        'shift_end': shift_end.isoformat(),
        'case_id': row['case_id'],
        'code': row['code'],
        'current_event_id': row['event_id'],
        'current_score': round(row['current_score'], 5),
        'event_at': row['event_at'].isoformat() if row.get('event_at') else None,
        'history_scores': [round(score, 5) for score in row['history_scores']],
        'history_count': len(row['history_scores']),
        'history_mean': row['history_mean'],
        'history_std': row['history_std'],
        'signed_deviation': row['signed_deviation'],
        'deviation': row['deviation'],
        'deviation_method': row['deviation_method'],
        'slot_no': row['slot_no'],
        'rank_reason': (
            f"班前历史异常分 {len(row['history_scores'])} 条，均值 {row['history_mean']}，"
            f"当班最新分 {round(row['current_score'], 5)}，标准化偏离 {row['deviation']}，"
            f"例行第 {row['slot_no']} 席（每柜最多一席，共 {ROUTINE_SLOT_LIMIT} 席）"
        ),
    }


def _expedited_basis(row):
    violations = []
    if not (float(row['target_temp_min']) <= float(row['temperature']) <= float(row['target_temp_max'])):
        violations.append('temperature')
    if not (float(row['target_rh_min']) <= float(row['humidity']) <= float(row['target_rh_max'])):
        violations.append('humidity')
    labels = {'temperature': '温度', 'humidity': '相对湿度'}
    ranges = {
        'temperature': f"{row['target_temp_min']}—{row['target_temp_max']} °C",
        'humidity': f"{row['target_rh_min']}—{row['target_rh_max']} %",
    }
    values = {'temperature': f"{row['temperature']} °C", 'humidity': f"{row['humidity']} %"}
    reasons = [f"{labels[key]} {values[key]} 越出保护范围 {ranges[key]}" for key in violations]
    door_note = '（门磁开启段，仍按加急处理）' if row['door_open'] else ''
    return {
        'sample_id': row['sample_id'],
        'case_id': row['case_id'],
        'code': row['code'],
        'sampled_at': row['sampled_at'].isoformat(),
        'temperature': float(row['temperature']),
        'humidity': float(row['humidity']),
        'door_open': bool(row['door_open']),
        'target_temp_min': float(row['target_temp_min']),
        'target_temp_max': float(row['target_temp_max']),
        'target_rh_min': float(row['target_rh_min']),
        'target_rh_max': float(row['target_rh_max']),
        'violations': violations,
        'door_open': bool(row['door_open']),
        'rank_reason': '；'.join(reasons) + f'{door_note}，加急处理，不占例行名额',
    }


def plan_routine(shift_start, shift_end, user):
    """重跑安全的例行排程：改派未出发安排，保留已出发安排及其当时依据。"""
    ensure_schema()
    shift_start, shift_end = as_utc(shift_start), as_utc(shift_end)
    lock_key = int(abs(shift_start.timestamp())) % 2_000_000_000
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute('SELECT pg_advisory_xact_lock(%s, %s)', (ADVISORY_KEY, lock_key))
            cur.execute(
                """SELECT id, case_id, status, slot_no, basis FROM night_round_tasks
                   WHERE shift_start=%s AND round_kind='routine' FOR UPDATE""",
                (shift_start,),
            )
            existing = _dicts(cur)
            departed = {row['case_id']: row for row in existing if row['status'] == 'departed'}
            pending = {row['case_id']: row for row in existing if row['status'] == 'pending'}

            candidates = fetch_candidates(cur, shift_start, shift_end)
            # 已出发柜不再参与重排，其名额继续占用
            open_candidates = [row for row in candidates if row['case_id'] not in departed]
            chosen, remaining = rank_routine(
                open_candidates, limit=max(ROUTINE_SLOT_LIMIT - len(departed), 0)
            )
            chosen_cases = {row['case_id'] for row in chosen}

            # 已出发任务保留出发时位次并继续占名额；新安排只填补剩余最低位次
            kept_departed = list(departed.values())
            taken_slots = {task['slot_no'] for task in kept_departed}
            free_slots = [slot for slot in range(1, ROUTINE_SLOT_LIMIT + 1) if slot not in taken_slots]
            created, updated, superseded = [], [], []
            for offset, row in enumerate(chosen):
                row['slot_no'] = free_slots[offset]
                basis = json.dumps(_routine_basis(row, shift_start, shift_end), ensure_ascii=False)
                if row['case_id'] in pending:
                    task = pending[row['case_id']]
                    cur.execute(
                        """UPDATE night_round_tasks
                           SET slot_no=%s, basis=%s::jsonb, status='pending', updated_at=NOW()
                           WHERE id=%s RETURNING *""",
                        (row['slot_no'], basis, task['id']),
                    )
                    updated.append(dict(cur.fetchone()))
                else:
                    cur.execute(
                        """INSERT INTO night_round_tasks
                           (shift_start, shift_end, case_id, round_kind, status, slot_no, basis, created_by)
                           VALUES (%s,%s,%s,'routine','pending',%s,%s::jsonb,%s) RETURNING *""",
                        (shift_start, shift_end, row['case_id'], row['slot_no'], basis, user['id']),
                    )
                    created.append(dict(cur.fetchone()))

            for case_id, task in pending.items():
                if case_id not in chosen_cases:
                    cur.execute(
                        """UPDATE night_round_tasks
                           SET status='superseded', updated_at=NOW() WHERE id=%s RETURNING *""",
                        (task['id'],),
                    )
                    superseded.append(dict(cur.fetchone()))

            result = {
                'shift_start': shift_start, 'shift_end': shift_end,
                'created': created, 'updated': updated,
                'superseded': superseded, 'kept_departed': kept_departed,
                'candidate_ranking': chosen + remaining,
                'audit': {
                    'action': 'plan_night_routine',
                    'detail': {
                        'shift_start': shift_start.isoformat(),
                        'created': [task['id'] for task in created],
                        'updated': [task['id'] for task in updated],
                        'superseded': [task['id'] for task in superseded],
                        'kept_departed': [task['id'] for task in kept_departed],
                    },
                },
            }
            cur.execute(
                """INSERT INTO audit_logs (actor_id, action, entity_type, entity_id, detail)
                   VALUES (%s, 'plan_night_routine', 'night_round_task', NULL, %s::jsonb)""",
                (user['id'], json.dumps(result['audit']['detail'], ensure_ascii=False)),
            )
            return result


def sync_expedited(shift_start, shift_end, user):
    """温湿越限即加急；同班次同柜已有在途加急单则不新增（不占例行名额）。"""
    ensure_schema()
    shift_start, shift_end = as_utc(shift_start), as_utc(shift_end)
    lock_key = int(abs(shift_start.timestamp())) % 2_000_000_000
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute('SELECT pg_advisory_xact_lock(%s, %s)', (ADVISORY_KEY, lock_key))
            cur.execute(
                """SELECT case_id FROM night_round_tasks
                   WHERE shift_start=%s AND round_kind='expedited'
                     AND status IN ('pending','departed')""",
                (shift_start,),
            )
            active_cases = {row['case_id'] for row in cur.fetchall()}
            created = []
            for row in fetch_violations(cur, shift_start, shift_end):
                if row['case_id'] in active_cases:
                    continue
                basis = json.dumps(_expedited_basis(row), ensure_ascii=False)
                cur.execute(
                    """INSERT INTO night_round_tasks
                       (shift_start, shift_end, case_id, round_kind, status, slot_no, basis, created_by)
                       VALUES (%s,%s,%s,'expedited','pending',NULL,%s::jsonb,%s)
                       ON CONFLICT DO NOTHING RETURNING *""",
                    (shift_start, shift_end, row['case_id'], basis, user['id']),
                )
                inserted = cur.fetchone()
                if inserted:
                    created.append(dict(inserted))
            if created:
                cur.execute(
                    """INSERT INTO audit_logs (actor_id, action, entity_type, entity_id, detail)
                       VALUES (%s, 'expedite_night_round', 'night_round_task', NULL, %s::jsonb)""",
                    (user['id'], json.dumps({
                        'shift_start': shift_start.isoformat(),
                        'created': [task['id'] for task in created],
                    }, ensure_ascii=False)),
                )
            return {'shift_start': shift_start, 'shift_end': shift_end, 'created': created}


def confirm_departed(task_id, user):
    """出发确认：与改派互斥；被换下(superseded)的安排不能确认。"""
    ensure_schema()
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute('SELECT * FROM night_round_tasks WHERE id=%s FOR UPDATE', (task_id,))
            row = cur.fetchone()
            if not row:
                return None
            task = dict(row)
            if task['status'] == 'departed':
                return task
            if task['status'] == 'superseded':
                raise Conflict('该安排已在改派中被换下，不能确认出发')
            cur.execute(
                """UPDATE night_round_tasks
                   SET status='departed', departed_at=NOW(), departed_by=%s, updated_at=NOW()
                   WHERE id=%s RETURNING *""",
                (user['id'], task_id),
            )
            departed = dict(cur.fetchone())
            cur.execute(
                """INSERT INTO audit_logs (actor_id, action, entity_type, entity_id, detail)
                   VALUES (%s, 'depart_night_round', 'night_round_task', %s, %s::jsonb)""",
                (user['id'], task_id, json.dumps({
                    'case_id': departed['case_id'], 'round_kind': departed['round_kind'],
                    'status': departed['status'], 'basis': departed['basis'],
                }, ensure_ascii=False, default=str)),
            )
            return departed


def list_tasks(shift_start, shift_end):
    ensure_schema()
    shift_start, shift_end = as_utc(shift_start), as_utc(shift_end)
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """SELECT t.*, c.code, c.artifact_title, c.gallery,
                          u.display_name AS created_by_name,
                          d.display_name AS departed_by_name
                   FROM night_round_tasks t
                   JOIN display_cases c ON c.id=t.case_id
                   LEFT JOIN users u ON u.id=t.created_by
                   LEFT JOIN users d ON d.id=t.departed_by
                   WHERE t.shift_start=%s
                   ORDER BY t.round_kind, COALESCE(t.slot_no, 0), t.status, t.id""",
                (shift_start,),
            )
            tasks = _dicts(cur)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            candidates = fetch_candidates(cur, shift_start, shift_end)
            violations = fetch_violations(cur, shift_start, shift_end)
    chosen, remaining = rank_routine(candidates, limit=ROUTINE_SLOT_LIMIT)
    return {
        'shift_start': shift_start,
        'shift_end': shift_end,
        'routine_slot_limit': ROUTINE_SLOT_LIMIT,
        'tasks': tasks,
        'ranking': chosen,
        'excluded_or_unselected': remaining,
        'climate_violations': violations,
    }


class Conflict(Exception):
    """业务并发冲突，路由层映射为 409。"""
