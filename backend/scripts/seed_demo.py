import hashlib
import os
from datetime import datetime, timedelta, timezone

from src.utils.db import execute, execute_returning, fetch_one, wait_for_db


def password(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def get_user(username, display_name, role):
    row = execute_returning(
        '''INSERT INTO users (username, display_name, role, password_hash)
           VALUES (%s,%s,%s,%s)
           ON CONFLICT (username) DO UPDATE SET display_name=EXCLUDED.display_name, role=EXCLUDED.role
           RETURNING id, username, display_name, role''',
        (username, display_name, role, password('museum123')),
    )
    return row or fetch_one('SELECT id, username, display_name, role FROM users WHERE username=%s', (username,))


def get_case(data):
    row = execute_returning(
        '''INSERT INTO display_cases
           (code, gallery, zone, artifact_code, artifact_title, target_temp_min, target_temp_max,
            target_rh_min, target_rh_max, status, door_sensor_id)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (code) DO UPDATE SET gallery=EXCLUDED.gallery, zone=EXCLUDED.zone,
             artifact_code=EXCLUDED.artifact_code, artifact_title=EXCLUDED.artifact_title
           RETURNING id''',
        data,
    )
    return row['id'] if row else fetch_one('SELECT id FROM display_cases WHERE code=%s', (data[0],))['id']


def sample(case_id, when, temp, rh, co2, door, note):
    row = execute_returning(
        '''INSERT INTO environment_samples
           (case_id, sampled_at, temperature, humidity, co2_ppm, door_open, source, state, note, created_by)
           VALUES (%s,%s,%s,%s,%s,%s,'seed','accepted',%s,NULL)
           ON CONFLICT (case_id, sampled_at) DO NOTHING RETURNING id''',
        (case_id, when, temp, rh, co2, door, note),
    )
    if row:
        return row['id']
    return fetch_one('SELECT id FROM environment_samples WHERE case_id=%s AND sampled_at=%s', (case_id, when))['id']


def seed_event(case_id, sample_id, score, threshold, anomaly_type, severity, status, door_context, consecutive, escalated, reason):
    existing = fetch_one('SELECT id FROM anomaly_events WHERE sample_id=%s', (sample_id,))
    if existing:
        return existing['id']
    row = execute_returning(
        '''INSERT INTO anomaly_events
           (case_id, sample_id, score, threshold, anomaly_type, severity, status, door_context,
            consecutive_count, escalated_to_curator, reason)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id''',
        (case_id, sample_id, score, threshold, anomaly_type, severity, status, door_context, consecutive, escalated, reason),
    )
    return row['id']


def main():
    wait_for_db()
    operator = get_user('lin-operator', '林芮 · 夜班值守', 'operator')
    supervisor = get_user('guo-supervisor', '郭岚 · 藏品主管', 'supervisor')
    get_user('viewer', '顾言 · 研究阅览', 'viewer')

    cases = {}
    cases['MC-01'] = get_case(('MC-01', '青铜器展厅', '东侧 02 号展柜', 'BR-018', '错金银云纹铜犀尊', 19.0, 23.0, 45.0, 55.0, 'active', 'door-mc01'))
    cases['MC-02'] = get_case(('MC-02', '丝路文物展厅', '西侧 07 号展柜', 'SR-044', '唐三彩骆驼俑', 18.0, 22.0, 42.0, 52.0, 'active', 'door-mc02'))
    cases['MC-03'] = get_case(('MC-03', '玉器展厅', '北侧 03 号展柜', 'JA-006', '汉代龙纹玉璧', 20.0, 24.0, 40.0, 50.0, 'active', 'door-mc03'))
    cases['MC-04'] = get_case(('MC-04', '瓷器展厅', '南侧 05 号展柜', 'PO-071', '北宋汝窑天青釉盘', 18.5, 22.5, 44.0, 54.0, 'active', 'door-mc04'))

    base = datetime(2026, 11, 21, 18, 0, tzinfo=timezone.utc)
    for index, (temp, rh, co2) in enumerate([(21.0, 49.0, 620), (21.2, 49.5, 640), (20.9, 48.8, 610), (21.1, 49.2, 630), (21.0, 49.1, 625), (21.2, 49.3, 635), (21.1, 49.0, 615), (21.0, 49.2, 628)]):
        sample(cases['MC-01'], base + timedelta(minutes=index * 10), temp, rh, co2, False, '夜班稳态基线')

    door_ids = []
    for index, values in enumerate([(20.4, 47.2, 720, False), (20.3, 47.0, 715, True), (22.8, 60.0, 1200, True), (22.5, 59.0, 1150, True), (21.0, 50.0, 700, False), (21.1, 49.6, 680, False), (21.0, 49.5, 675, False)]):
        door_ids.append(sample(cases['MC-02'], base + timedelta(minutes=index * 10), *values, '门磁开启期间的光照调整'))
    seed_event(cases['MC-02'], door_ids[2], -0.14, -0.10, 'door_interference', 'watch', 'open', True, 1, False, '门磁开启段内温湿度突变，等待关门后复采')

    drift_ids = []
    for index, values in enumerate([(21.2, 45.0, 650), (21.4, 45.2, 665), (23.8, 57.0, 1100), (24.4, 59.0, 1240), (24.8, 61.0, 1370), (21.8, 47.0, 700), (21.4, 46.0, 680)]):
        drift_ids.append(sample(cases['MC-03'], base + timedelta(minutes=index * 10), *values, False, '连续偏移观察样本'))
    seed_event(cases['MC-03'], drift_ids[4], -0.31, -0.12, 'continuous_drift', 'critical', 'escalated', False, 3, True, '连续三个采样点超出稳态基线，已升级馆长查看')

    for index, values in enumerate([(20.1, 50.0, 590), (20.0, 50.2, 605), (20.2, 49.8, 600), (20.1, 50.1, 610), (20.0, 49.9, 600), (20.2, 50.0, 615)]):
        sample(cases['MC-04'], base + timedelta(minutes=index * 10), *values, False, '日常巡检基线')
    # 当班温湿越出保护范围（MC-04 温度上限 22.5），用于演示温湿加急，不占例行名额
    mc04_violation = sample(cases['MC-04'], base + timedelta(minutes=70), 23.6, 50.4, 640, False, '当班温度越限，触发加急巡检')

    def pre_shift_event(case_key, sample_id, when, score, reason):
        existing = fetch_one('SELECT id FROM anomaly_events WHERE sample_id=%s', (sample_id,))
        if existing:
            return
        execute(
            '''INSERT INTO anomaly_events
               (case_id, sample_id, algorithm_run_id, score, threshold, anomaly_type, severity, status,
                door_context, consecutive_count, escalated_to_curator, reason, created_at)
               VALUES (%s,%s,NULL,%s,-0.10,'environment_outlier','high','closed',FALSE,1,FALSE,%s,%s)''',
            (cases[case_key], sample_id, score, reason, when),
        )

    # 班前（上一班 17:xx）历史异常分：各柜尺度不同，例行排程按各自基线算标准化偏离。
    # 与当班首采间隔 20 分钟，遵守 30 分钟采样间隔上限。
    pre_shift = base - timedelta(minutes=20)
    pre_shift_event('MC-01', sample(cases['MC-01'], pre_shift, 21.0, 49.0, 620, False, '班前历史采样'), pre_shift, -0.05, '班前历史离群（基线对照）')
    pre_shift_event('MC-01', sample(cases['MC-01'], pre_shift - timedelta(minutes=10), 20.9, 48.9, 615, False, '班前历史采样'), pre_shift - timedelta(minutes=10), -0.06, '班前历史离群（基线对照）')
    pre_shift_event('MC-03', sample(cases['MC-03'], pre_shift, 21.2, 45.0, 650, False, '班前历史采样'), pre_shift, -0.12, '班前历史离群（基线对照）')
    pre_shift_event('MC-03', sample(cases['MC-03'], pre_shift - timedelta(minutes=10), 21.1, 45.1, 648, False, '班前历史采样'), pre_shift - timedelta(minutes=10), -0.10, '班前历史离群（基线对照）')
    pre_shift_event('MC-04', sample(cases['MC-04'], pre_shift, 20.1, 50.0, 595, False, '班前历史采样'), pre_shift, -0.20, '班前历史离群（基线对照）')
    pre_shift_event('MC-04', sample(cases['MC-04'], pre_shift - timedelta(minutes=10), 20.0, 49.8, 590, False, '班前历史采样'), pre_shift - timedelta(minutes=10), -0.19, '班前历史离群（基线对照）')
    # 当班温湿越限对应的加急候选事件（无班前分数也不影响加急）
    execute(
        '''INSERT INTO anomaly_events
           (case_id, sample_id, algorithm_run_id, score, threshold, anomaly_type, severity, status,
            door_context, consecutive_count, escalated_to_curator, reason, created_at)
           SELECT %s,%s,NULL,-0.22,-0.10,'environment_outlier','high','open',FALSE,1,FALSE,%s,%s
           WHERE NOT EXISTS (SELECT 1 FROM anomaly_events WHERE sample_id=%s)''',
        (cases['MC-04'], mc04_violation, '当班温度越出保护范围，等待加急巡检', base + timedelta(minutes=70), mc04_violation),
    )

    execute("UPDATE display_cases SET last_sample_at=(SELECT MAX(sampled_at) FROM environment_samples WHERE case_id=display_cases.id)")
    execute(
        '''INSERT INTO audit_logs (actor_id, action, entity_type, entity_id, detail)
           SELECT %s, 'seed_demo', 'batch', NULL, '{"source":"seed_demo","batch":"night-shift-2026-11-21"}'::jsonb
           WHERE NOT EXISTS (SELECT 1 FROM audit_logs WHERE action='seed_demo' AND detail->>'batch'='night-shift-2026-11-21')''',
        (operator['id'],),
    )
    print('seed_demo complete: 4 display cases, 35 samples, 9 anomaly events (含 6 条班前历史对照), 3 users')


if __name__ == '__main__':
    main()
