-- 夜班巡检排程：例行名额（按班前历史分数偏离）与温湿加急（不占名额）
CREATE TABLE IF NOT EXISTS night_round_tasks (
    id SERIAL PRIMARY KEY,
    shift_start TIMESTAMPTZ NOT NULL,
    shift_end TIMESTAMPTZ NOT NULL,
    case_id INTEGER NOT NULL REFERENCES display_cases(id) ON DELETE CASCADE,
    round_kind VARCHAR(20) NOT NULL CHECK (round_kind IN ('routine','expedited')),
    status VARCHAR(20) NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','superseded','departed')),
    -- 例行名额内的位次（1 起）；加急不占名额，恒为 NULL
    slot_no INTEGER,
    -- 入选/换下依据快照，已出发的安排永久保留当时依据
    basis JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_by INTEGER REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    departed_at TIMESTAMPTZ,
    departed_by INTEGER REFERENCES users(id),
    CHECK ((round_kind='routine') = (slot_no IS NOT NULL))
);

-- 同一班次、同一展柜、同一类型只允许一条在途（待出发/已出发）安排，保证重跑幂等
CREATE UNIQUE INDEX IF NOT EXISTS uniq_night_round_active
    ON night_round_tasks(shift_start, case_id, round_kind)
    WHERE status IN ('pending','departed');

CREATE INDEX IF NOT EXISTS idx_night_round_shift ON night_round_tasks(shift_start, round_kind, status);
