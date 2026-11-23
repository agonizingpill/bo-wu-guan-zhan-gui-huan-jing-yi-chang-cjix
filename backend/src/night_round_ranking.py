"""夜班例行巡检的偏离排序口径（纯函数，便于单测）。

不能直接比较各柜异常分的绝对值：每柜传感器基线不同，分数尺度不可横向比较。
因此用每柜**班前历史异常分**建立自身基线，衡量当班最新异常分相对该基线的
标准化偏离（z 分数）；没有班前历史分的展柜不进入例行候选。
"""

ROUTINE_SLOT_LIMIT = 2


def z_deviation(current, history):
    """返回 (绝对偏离, 带符号偏离, 均值, 样本标准差, 口径说明)。

    history 为班前历史异常分列表。样本不足或标准差为 0 时退化为均值差，
    避免除零，并在口径说明中标记。
    """
    if not history:
        return None
    count = len(history)
    mean = sum(history) / count
    variance = sum((value - mean) ** 2 for value in history) / (count - 1) if count >= 2 else 0.0
    std = variance ** 0.5
    if std > 0:
        signed = (current - mean) / std
        method = 'z_score'
    else:
        signed = current - mean
        method = 'mean_delta'
    return abs(signed), signed, mean, std, method


def rank_routine(candidates, limit=ROUTINE_SLOT_LIMIT):
    """按标准化偏离降序选例行柜，每柜最多一席。

    candidates: [{case_id, code, current_score, history_scores, ...透传字段}]
    无班前历史分的候选直接剔除。偏离相同按 case_id 升序，保证结果确定。
    返回 (入选列表, 未入选/被剔除列表)，入选列表附带 deviation 与 rank。
    """
    ranked = []
    excluded = []
    for item in candidates:
        history = list(item.get('history_scores') or [])
        if not history:
            excluded.append({**item, 'exclude_reason': 'no_pre_shift_score'})
            continue
        deviation = z_deviation(item['current_score'], history)
        abs_dev, signed, mean, std, method = deviation
        ranked.append({
            **item,
            'deviation': round(abs_dev, 6),
            'signed_deviation': round(signed, 6),
            'history_mean': round(mean, 6),
            'history_std': round(std, 6),
            'deviation_method': method,
        })
    ranked.sort(key=lambda row: (-row['deviation'], row['case_id']))
    chosen, rest = ranked[:limit], ranked[limit:]
    for index, row in enumerate(chosen, start=1):
        row['slot_no'] = index
    return chosen, rest + excluded
