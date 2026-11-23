/* 夜班巡检排程页面：例行两席（班前历史偏离）+ 温湿加急（不占名额） */

const nightState = {
  view: null,
  windows: null,
  selectedShift: '',
  loading: false
};

function nightToken() {
  return localStorage.getItem('museum_token');
}

async function nightApi(path, options = {}) {
  const response = await fetch(`/api/night-rounds${path}`, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...(options.headers || {}),
      ...(nightToken() ? { Authorization: `Bearer ${nightToken()}` } : {})
    }
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `请求失败 (${response.status})`);
  return data;
}

const nightFmt = (value) => value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '—';
const nightEsc = (value) => String(value ?? '').replace(/[&<>'"]/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[char]));
const roundLabel = { routine: '例行巡检', expedited: '温湿加急' };
const taskStatusLabel = { pending: '待出发', superseded: '已改派换下', departed: '已出发' };

async function loadNightRounds() {
  if (!nightToken()) return;
  nightState.loading = true;
  try {
    if (!nightState.windows) nightState.windows = await nightApi('/shift-windows');
    if (!nightState.selectedShift) {
      nightState.selectedShift = nightState.windows.current_window.shift_start;
    }
    nightState.view = await nightApi(`?shift_start=${encodeURIComponent(nightState.selectedShift)}`);
  } catch (error) {
    nightState.view = { error: error.message };
  } finally {
    nightState.loading = false;
    renderNightRounds();
  }
}

function shiftOptions() {
  const windows = nightState.windows?.windows || [];
  return windows.map((window) => {
    const start = new Date(window.shift_start);
    const label = `${start.getMonth() + 1}月${start.getDate()}日 18:00 夜班`;
    return `<option value="${nightEsc(window.shift_start)}" ${nightEsc(window.shift_start) === nightState.selectedShift ? 'selected' : ''}>${label}</option>`;
  }).join('');
}

function routineTasks() {
  return (nightState.view?.tasks || []).filter((task) => task.round_kind === 'routine');
}

function activeRoutine() {
  return routineTasks().filter((task) => ['pending', 'departed'].includes(task.status));
}

function taskCard(task) {
  const basis = task.basis || {};
  const departed = task.status === 'departed';
  const replaced = task.status === 'superseded';
  const evidence = task.round_kind === 'routine'
    ? `班前历史 ${basis.history_count ?? 0} 条 · 均值 ${basis.history_mean ?? '—'} · 当班分 ${basis.current_score ?? '—'} · 标准化偏离 <b>${basis.deviation ?? '—'}</b>`
    : basis.rank_reason || '温湿越出保护范围';
  return `<article class="round-card ${task.round_kind} ${task.status}">
    <div class="round-card-head">
      <span class="round-kind">${roundLabel[task.round_kind]}${task.slot_no ? ` · 第 ${task.slot_no} 席` : ''}</span>
      <span class="round-status ${task.status}">${taskStatusLabel[task.status] || task.status}</span>
    </div>
    <h3>${nightEsc(task.code)} · ${nightEsc(task.artifact_title)}</h3>
    <p class="round-evidence">${task.round_kind === 'routine' ? evidence : nightEsc(evidence)}</p>
    <p class="round-basis">${nightEsc(basis.rank_reason || '')}</p>
    <div class="round-meta">
      <span>登记 ${nightFmt(task.created_at)}</span>
      ${departed ? `<span>出发 ${nightFmt(task.departed_at)} · ${nightEsc(task.departed_by_name || '')}</span>` : ''}
      ${replaced ? '<span>改派后该安排不再执行，依据保留备查</span>' : ''}
    </div>
    ${task.status === 'pending' ? `<button class="button primary night-depart" data-task="${task.id}">确认出发</button>` : ''}
  </article>`;
}

function rankingRows() {
  const view = nightState.view || {};
  const rows = [...(view.ranking || []), ...(view.excluded_or_unselected || [])];
  if (!rows.length) return '<p class="round-empty">当班没有具备班前历史分的异常展柜，暂不生成例行安排。</p>';
  return `<div class="round-table"><div class="round-table-head"><span>展柜</span><span>当班分</span><span>班前均值/标准差</span><span>标准化偏离</span><span>结论</span></div>
    ${rows.map((row) => {
      const excluded = row.exclude_reason === 'no_pre_shift_score';
      const picked = row.slot_no != null;
      const verdict = excluded ? '无班前历史分，不排例行' : picked ? `入选第 ${row.slot_no} 席` : '未入选（名额已满）';
      return `<div class="round-table-row ${excluded ? 'excluded' : picked ? 'picked' : ''}">
        <span>${nightEsc(row.code || '')}</span>
        <span>${row.current_score ?? '—'}</span>
        <span>${excluded ? '—' : `${row.history_mean} / ${row.history_std}`}</span>
        <span><b>${excluded ? '—' : row.deviation}</b><small>${row.deviation_method || ''}</small></span>
        <span>${verdict}</span>
      </div>`;
    }).join('')}</div>`;
}

function renderNightRounds() {
  const host = document.querySelector('#night-rounds-page');
  if (!host) return;
  if (nightState.loading && !nightState.view) {
    host.innerHTML = '<div class="panel round-panel"><p class="eyebrow">NIGHT ROUNDS</p><p>正在加载巡检安排…</p></div>';
    return;
  }
  if (nightState.view?.error) {
    host.innerHTML = `<div class="panel round-panel"><p class="eyebrow">NIGHT ROUNDS</p><p class="round-empty">${nightEsc(nightState.view.error)}</p></div>`;
    return;
  }
  const view = nightState.view || {};
  const limit = view.routine_slot_limit ?? 2;
  const used = activeRoutine().length;
  const expedited = (view.tasks || []).filter((task) => task.round_kind === 'expedited');
  host.innerHTML = `
    <div class="panel round-panel">
      <div class="panel-head">
        <div><p class="eyebrow">NIGHT ROUNDS / 例行 ${used}/${limit} 席 · 每柜最多一席</p>
          <h2>夜班巡检安排</h2></div>
        <div class="round-controls">
          <select id="night-shift-select" aria-label="班次窗口">${shiftOptions()}</select>
          <button class="button" id="night-plan-routine">重排例行（按班前偏离）</button>
          <button class="button amber" id="night-sync-expedited">同步温湿加急</button>
        </div>
      </div>
      <p class="round-rule">各柜异常分尺度不同，不直接比绝对值；以每柜班前历史异常分为基线计算标准化偏离，偏离最大的柜优先。温湿越出保护范围立即加急，不占用例行名额。</p>
      <div class="round-summary">
        <span>例行名额 <b>${used}</b>/${limit}</span>
        <span>待出发 <b>${view.tasks?.filter((t) => t.status === 'pending').length || 0}</b></span>
        <span>已出发 <b>${view.tasks?.filter((t) => t.status === 'departed').length || 0}</b></span>
        <span>温湿加急 <b>${expedited.filter((t) => t.status !== 'superseded').length}</b></span>
      </div>
    </div>
    <div class="round-sections">
      <section class="panel round-panel">
        <div class="panel-head"><div><p class="eyebrow">ROUTINE / 入选依据可追溯</p><h3>例行巡检</h3></div></div>
        <div class="round-cards">${routineTasks().length ? routineTasks().map(taskCard).join('') : '<p class="round-empty">暂无例行安排，点击「重排例行」生成。无班前历史分的展柜不会入选。</p>'}</div>
      </section>
      <section class="panel round-panel expedited-panel">
        <div class="panel-head"><div><p class="eyebrow">EXPEDITED / 不等评分</p><h3>温湿加急</h3></div></div>
        <div class="round-cards">${expedited.length ? expedited.map(taskCard).join('') : '<p class="round-empty">当班暂无温湿越限采样。</p>'}</div>
      </section>
    </div>
    <section class="panel round-panel">
      <div class="panel-head"><div><p class="eyebrow">SELECTION BASIS / 全量候选</p><h3>入选依据与未入选说明</h3></div></div>
      ${rankingRows()}
    </section>`;
  bindNightRounds();
}

function bindNightRounds() {
  document.querySelector('#night-shift-select')?.addEventListener('change', (event) => {
    nightState.selectedShift = event.target.value;
    loadNightRounds();
  });
  document.querySelector('#night-plan-routine')?.addEventListener('click', async () => {
    try {
      const result = await nightApi('/routine/plan', {
        method: 'POST',
        body: JSON.stringify({ shift_start: nightState.selectedShift })
      });
      window.dispatchEvent(new CustomEvent('museum-notice', {
        detail: `例行重排：新增 ${result.created.length} · 改派 ${result.updated.length} · 换下 ${result.superseded.length} · 已出发保留 ${result.kept_departed.length}`
      }));
      await loadNightRounds();
    } catch (error) {
      window.dispatchEvent(new CustomEvent('museum-notice', { detail: error.message }));
    }
  });
  document.querySelector('#night-sync-expedited')?.addEventListener('click', async () => {
    try {
      const result = await nightApi('/expedited/sync', {
        method: 'POST',
        body: JSON.stringify({ shift_start: nightState.selectedShift })
      });
      window.dispatchEvent(new CustomEvent('museum-notice', {
        detail: result.created.length ? `已生成 ${result.created.length} 张温湿加急单（不占例行名额）` : '没有新的温湿越限，加急单无需新增'
      }));
      await loadNightRounds();
    } catch (error) {
      window.dispatchEvent(new CustomEvent('museum-notice', { detail: error.message }));
    }
  });
  document.querySelectorAll('.night-depart').forEach((button) => button.addEventListener('click', async () => {
    button.disabled = true;
    try {
      await nightApi(`/${button.dataset.task}/depart`, { method: 'POST' });
      window.dispatchEvent(new CustomEvent('museum-notice', { detail: '出发已确认，安排与当时依据已锁定' }));
      await loadNightRounds();
    } catch (error) {
      button.disabled = false;
      window.dispatchEvent(new CustomEvent('museum-notice', { detail: error.message }));
    }
  }));
}

window.addEventListener('museum-load-night-rounds', loadNightRounds);
