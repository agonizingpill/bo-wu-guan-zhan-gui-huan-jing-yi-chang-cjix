"""夜班巡检排程路由：例行两席（按班前历史偏离）+ 温湿加急（不占名额）。"""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import night_round_store as store
from .middlewares.auth import current_user, require_roles
from .utils.db import clean, get_conn

router = APIRouter(prefix='/night-rounds', tags=['夜班巡检排程'])


class RoutinePlanPayload(BaseModel):
    shift_start: datetime | None = None


class ExpeditedSyncPayload(BaseModel):
    shift_start: datetime | None = None


def resolve_window(shift_start):
    if shift_start is None:
        return store.default_shift_window()
    try:
        return store.window_for(shift_start)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get('/shift-windows')
def shift_windows(user=Depends(current_user)):
    """返回当前班次与库内已有安排/异常涉及的班次窗口，供页面切换。"""
    current_start, current_end = store.default_shift_window()
    windows = {current_start}
    store.ensure_schema()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT DISTINCT shift_start FROM night_round_tasks')
            windows.update(row[0] for row in cur.fetchall())
            cur.execute(
                """SELECT DISTINCT date_trunc('day', created_at) + INTERVAL '18 hours'
                   FROM anomaly_events
                   WHERE EXTRACT(HOUR FROM created_at) >= 18 OR EXTRACT(HOUR FROM created_at) < 7"""
            )
            windows.update(row[0] for row in cur.fetchall())
    ordered = sorted(windows, reverse=True)[:12]
    return {
        'current_window': {'shift_start': current_start, 'shift_end': current_end},
        'windows': [
            {'shift_start': start, 'shift_end': store.window_for(start)[1]} for start in ordered
        ],
    }


@router.get('')
def list_rounds(shift_start: datetime | None = None, user=Depends(current_user)):
    start, end = resolve_window(shift_start)
    return clean(store.list_tasks(start, end))


@router.post('/routine/plan', status_code=201)
def plan_routine(payload: RoutinePlanPayload, user=Depends(require_roles('operator', 'supervisor'))):
    start, end = resolve_window(payload.shift_start)
    # 审计随排程在同一事务写入，随回滚一起撤销
    return clean(store.plan_routine(start, end, user))


@router.post('/expedited/sync', status_code=201)
def sync_expedited(payload: ExpeditedSyncPayload, user=Depends(require_roles('operator', 'supervisor'))):
    start, end = resolve_window(payload.shift_start)
    return clean(store.sync_expedited(start, end, user))


@router.post('/{task_id}/depart')
def depart(task_id: int, user=Depends(require_roles('operator', 'supervisor'))):
    try:
        task = store.confirm_departed(task_id, user)
    except store.Conflict as exc:
        raise HTTPException(409, str(exc)) from exc
    if task is None:
        raise HTTPException(404, '巡检安排不存在')
    return clean(task)
