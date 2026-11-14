import { api } from '../api.js';
import { ENUMS, can, directionTone, reviewTone } from '../config.js';
import {
  badge,
  drawer,
  emptyState,
  errorBox,
  errorMessage,
  esc,
  field,
  fmtP,
  fmtTime,
  pct,
  signedPct,
  statStrip,
  table,
  toast,
} from '../ui.js';

const DIRECTIONS = ENUMS.direction;

function directionBadge(direction) {
  return badge(directionTone(direction), DIRECTIONS[direction] || direction);
}

function blockerList(blockers) {
  if (!blockers || !blockers.length) return '<span class="mono muted">无阻塞项</span>';
  return `<div style="display:flex;gap:6px;flex-wrap:wrap">${blockers
    .map((item) => badge('stop', esc(item)))
    .join('')}</div>`;
}

/** 口径登记 / 重登抽屉（仅草稿期可提交，开始后服务端 409 锁定）。 */
function openPlanDrawer(view, ctx, detail, refresh) {
  const plan = detail.plan;
  const metricOptions = Object.entries(ENUMS.metric).map(([value, label]) => ({ value, label }));
  const channelOptions = Object.entries(ENUMS.channel);
  const checkedGuardrails = new Set((plan?.guardrail_metrics || []).map((item) => item.metric_key));
  const checkedChannels = new Set((plan?.required_channels || []).map((item) => item.channel));
  const { root, close } = drawer({
    title: plan ? '重登评审口径' : '登记评审口径',
    subtitle: `${detail.experiment_code} · 实验开始前登记，开始后锁定`,
    body: `<form id="plan-form" style="display:grid;gap:12px">
      ${field({
        label: '主指标（缺省取实验登记的主指标）',
        name: 'primary_metric',
        value: plan?.primary_metric || '',
        options: [{ value: '', label: '— 跟随实验主指标 —' }, ...metricOptions],
      })}
      <div class="field"><label>护栏指标（恶化即拦截）</label>
        <div style="display:flex;gap:12px;flex-wrap:wrap;padding:6px 0">
          ${Object.entries(ENUMS.metric)
            .map(
              ([value, label]) => `<label style="display:flex;gap:4px;align-items:center;font-weight:400">
                <input type="checkbox" name="guardrail" value="${esc(value)}" ${checkedGuardrails.has(value) ? 'checked' : ''}/> ${esc(label)}
              </label>`,
            )
            .join('')}
        </div>
      </div>
      <div class="field"><label>必验渠道（分开验收，任一显著负向即拦截）</label>
        <div style="display:flex;gap:12px;flex-wrap:wrap;padding:6px 0">
          ${channelOptions
            .map(
              ([value, label]) => `<label style="display:flex;gap:4px;align-items:center;font-weight:400">
                <input type="checkbox" name="channel" value="${esc(value)}" ${checkedChannels.has(value) ? 'checked' : ''}/> ${esc(label)}
              </label>`,
            )
            .join('')}
        </div>
      </div>
      ${field({ label: '显著性水平 α', name: 'alpha', type: 'number', value: plan?.alpha ?? 0.05, attrs: 'step="0.01" min="0.001" max="0.49"' })}
      ${field({ label: '备注', name: 'note', value: plan?.note || '', placeholder: '口径来源、验收标准……' })}
      <div class="form-error" id="plan-error"></div>
      <button class="btn accent" type="submit">写入口径</button>
    </form>
    <div class="note" style="margin-top:10px">口径一旦登记，跑评审与发布都只能按它来；实验开始后此处锁定（服务端 409）。</div>`,
  });
  const form = root.querySelector('#plan-form');
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const errorNode = root.querySelector('#plan-error');
    const guardrails = [...form.querySelectorAll('input[name=guardrail]:checked')].map((node) => node.value);
    const channels = [...form.querySelectorAll('input[name=channel]:checked')].map((node) => node.value);
    const payload = {
      guardrail_metrics: guardrails,
      required_channels: channels,
      alpha: Number(form.alpha.value) || 0.05,
      note: form.note.value.trim() || null,
    };
    if (form.primary_metric.value) payload.primary_metric = form.primary_metric.value;
    try {
      await api.put(`/api/experiments/${detail.experiment_id}/review-plan`, payload);
      toast('评审口径已登记');
      close();
      await refresh();
    } catch (err) {
      errorNode.textContent = err.code === 'CONFLICT' ? '实验已开始，口径已锁定（409）' : errorMessage(err);
    }
  });
}

function planPanel(detail, ctx, refresh, view) {
  const plan = detail.plan;
  const canWrite = can(ctx.user.role, 'review:write');
  const action = canWrite && detail.can_register;
  const headAction = action
    ? `<button class="btn sm accent" id="edit-plan">${plan ? '重登口径' : '登记口径'}</button>`
    : `<span class="mono muted">${plan ? (detail.can_register ? '草稿期 · 需负责人修改' : '实验已开始 · 口径已锁定') : '未登记'}</span>`;
  const body = plan
    ? `<div class="checkline">
        <div class="item"><span class="k">主指标</span><span class="v">${esc(plan.primary_metric_label)}</span></div>
        <div class="item"><span class="k">显著性 α</span><span class="v">${esc(plan.alpha)}</span></div>
        <div class="item"><span class="k">登记人</span><span class="v">${esc(plan.registered_by)}</span></div>
        <div class="item"><span class="k">登记时间</span><span class="v">${fmtTime(plan.registered_at)}</span></div>
      </div>
      <div class="panel-body" style="padding-top:0;display:grid;gap:6px">
        <div><b>护栏指标</b>：${plan.guardrail_metrics.length ? plan.guardrail_metrics.map((item) => esc(item.metric_name)).join('、') : '<span class="muted">无</span>'}</div>
        <div><b>必验渠道</b>：${plan.required_channels.map((item) => esc(item.channel_label)).join('、')}</div>
        ${plan.note ? `<div class="note">${esc(plan.note)}</div>` : ''}
      </div>`
    : emptyState({
        title: '还没有评审口径',
        hint: '实验开始前登记主指标、护栏指标和必验渠道；没登记口径不能跑评审。',
      });
  return `<section class="panel">
    <div class="panel-head"><h2>评审口径</h2>${headAction}</div>
    <div class="panel-body" style="display:grid;gap:10px">${body}</div>
  </section>`;
}

function runPanel(detail, ctx) {
  const run = detail.latest_run;
  const canWrite = can(ctx.user.role, 'review:write');
  const runButton = canWrite && detail.plan
    ? `<span style="display:inline-flex;gap:6px;align-items:center">
        ${
          detail.treatments.length > 1
            ? `<select id="run-variant" style="max-width:180px">${detail.treatments
                .map((item) => `<option value="${esc(item.code)}">${esc(item.code)}</option>`)
                .join('')}</select>`
            : ''
        }
        <button class="btn sm accent" id="run-review">跑评审</button>
      </span>`
    : `<span class="mono muted">${detail.plan ? '跑评审需实验负责人' : '先登记口径'}</span>`;
  if (!run) {
    return `<section class="panel">
      <div class="panel-head"><h2>评审结论</h2>${runButton}</div>
      <div class="panel-body">${emptyState({ title: '还没有评审记录', hint: '登记口径后跑第一次评审。' })}</div>
    </section>`;
  }
  const overall = run.overall;
  return `<section class="panel">
    <div class="panel-head">
      <h2>评审结论</h2>
      <div style="display:flex;gap:8px;align-items:center">
        ${badge(reviewTone(run.verdict), run.verdict_label)}
        ${runButton}
      </div>
    </div>
    <div class="panel-body" style="display:grid;gap:12px">
      ${statStrip([
        { label: '对照组 · 主指标', value: pct(overall.control_rate, 2) },
        { label: '实验组 · 主指标', value: pct(overall.variant_rate, 2) },
        { label: '绝对差', value: signedPct(overall.diff, 2) },
        { label: 'p 值', value: fmtP(overall.p_value) },
        { label: '整体判断', value: DIRECTIONS[overall.direction] || overall.direction, tone: overall.direction === 'up' ? '' : 'alarm' },
      ])}
      <div><b>阻塞项</b>：${blockerList(run.blockers)}</div>
      <div class="mono muted">依据指纹 ${esc(run.data_fingerprint.slice(0, 12))} · ${esc(run.computed_by)} · ${fmtTime(run.created_at)} · 变体 ${esc(run.variant_code)}</div>
    </div>
    <div class="panel-body" style="padding-top:0">
      <h3 style="margin:0 0 8px">必验渠道（同一口径分开验收）</h3>
      ${table({
        rows: run.channels,
        columns: [
          { key: 'channel_label', label: '渠道' },
          { key: 'control_rate', label: '对照', render: (row) => pct(row.control_rate, 2) },
          { key: 'variant_rate', label: '实验', render: (row) => pct(row.variant_rate, 2) },
          { key: 'diff', label: '绝对差', render: (row) => signedPct(row.diff, 2) },
          { key: 'p_value', label: 'p 值', render: (row) => fmtP(row.p_value) },
          { key: 'direction', label: '判断', render: (row) => directionBadge(row.direction) },
        ],
        empty: { title: '没有渠道数据', hint: '' },
      })}
    </div>
    <div class="panel-body" style="padding-top:0">
      <h3 style="margin:0 0 8px">护栏指标</h3>
      ${table({
        rows: run.guardrails,
        columns: [
          { key: 'metric_name', label: '指标' },
          { key: 'control_rate', label: '对照', render: (row) => pct(row.control_rate, 2) },
          { key: 'variant_rate', label: '实验', render: (row) => pct(row.variant_rate, 2) },
          { key: 'diff', label: '绝对差', render: (row) => signedPct(row.diff, 2) },
          { key: 'p_value', label: 'p 值', render: (row) => fmtP(row.p_value) },
          { key: 'direction', label: '判断', render: (row) => directionBadge(row.direction) },
        ],
        empty: { title: '未登记护栏指标', hint: '' },
      })}
    </div>
  </section>`;
}

function releasePanel(detail, ctx) {
  const release = detail.release;
  const run = detail.latest_run;
  const canWrite = can(ctx.user.role, 'review:write');
  const canRelease = canWrite && run && run.verdict === 'ship';
  const action = canRelease
    ? `<button class="btn sm accent" id="do-release">发布上线</button>`
    : `<span class="mono muted">${run && run.verdict === 'hold' ? '评审不可上线，不能发布' : '无可发布结论'}</span>`;
  const body = release
    ? `<div class="checkline">
        <div class="item"><span class="k">当前判定</span><span class="v">${badge(reviewTone(release.verdict), release.verdict_label)}</span></div>
        <div class="item"><span class="k">依据</span><span class="v">评审 #${esc(release.run_id)}</span></div>
        <div class="item"><span class="k">变体</span><span class="v">${esc(release.variant_code)}</span></div>
        <div class="item"><span class="k">发布人</span><span class="v">${esc(release.released_by)}</span></div>
        <div class="item"><span class="k">发布时间</span><span class="v">${fmtTime(release.released_at)}</span></div>
      </div>
      <div class="note" style="margin-top:8px">判定与依据一次性切换；发布失败整体回滚，线上保留旧版。</div>`
    : emptyState({ title: '尚未发布', hint: '最新评审结论为「可上线」时才能发布；判定与依据一次性切换。' });
  return `<section class="panel">
    <div class="panel-head"><h2>发布</h2>${action}</div>
    <div class="panel-body" style="display:grid;gap:10px">${body}</div>
  </section>`;
}

function historyPanel(detail) {
  return `<section class="panel">
    <div class="panel-head"><h2>评审历史</h2><span class="meta">${detail.runs.length} 次 · 只增不改</span></div>
    ${table({
      rows: detail.runs,
      columns: [
        { key: 'id', label: '#', className: 'mono' },
        { key: 'created_at', label: '时间', render: (row) => fmtTime(row.created_at) },
        { key: 'variant_code', label: '变体', className: 'mono' },
        { key: 'data_fingerprint', label: '数据指纹', className: 'mono', render: (row) => esc(row.data_fingerprint.slice(0, 12)) },
        { key: 'verdict', label: '结论', render: (row) => badge(reviewTone(row.verdict), row.verdict_label) },
        { key: 'blockers', label: '阻塞项', render: (row) => (row.blockers.length ? esc(row.blockers.join('；')) : '<span class="muted">—</span>') },
        { key: 'computed_by', label: '操作人', className: 'mono nowrap' },
      ],
      empty: { title: '没有评审记录', hint: '' },
    })}
  </section>`;
}

export const reviewsPage = {
  async render(view, ctx) {
    let states;
    try {
      states = await api.get('/api/reviews');
    } catch (err) {
      view.innerHTML = errorBox(errorMessage(err));
      view.querySelector('#retry-btn')?.addEventListener('click', () => reviewsPage.render(view, ctx));
      return;
    }
    const items = states.items || [];
    const fallbackId =
      items.find((item) => item.latest_run)?.experiment_id ||
      items.find((item) => item.plan_registered)?.experiment_id ||
      items[0]?.experiment_id ||
      0;
    const selectedId = Number(ctx.query.experiment || fallbackId);
    let detail = null;
    if (selectedId) {
      try {
        detail = await api.get(`/api/experiments/${selectedId}/review`);
      } catch (err) {
        view.innerHTML = errorBox(errorMessage(err));
        view.querySelector('#retry-btn')?.addEventListener('click', () => reviewsPage.render(view, ctx));
        return;
      }
    }
    const refresh = () => reviewsPage.render(view, ctx);

    view.innerHTML = `
      <section class="panel">
        <div class="panel-head">
          <h2>各实验评审状态</h2>
          <span class="meta">口径预登记 · 同批数据重跑不翻案 · 发布原子切换</span>
        </div>
        ${table({
          rows: items,
          columns: [
            { key: 'experiment_code', label: '实验', className: 'mono nowrap' },
            { key: 'experiment_name', label: '名称' },
            {
              key: 'plan_registered',
              label: '评审口径',
              render: (row) => (row.plan_registered ? badge('ok', '已登记') : badge('pending', '未登记')),
            },
            {
              key: 'latest_run',
              label: '最新评审',
              render: (row) =>
                row.latest_run
                  ? `${badge(reviewTone(row.latest_run.verdict), row.latest_run.verdict_label)} <span class="mono muted">${fmtTime(row.latest_run.created_at)}</span>`
                  : '<span class="muted">未评审</span>',
            },
            {
              key: 'release',
              label: '发布',
              render: (row) =>
                row.release
                  ? `${badge(reviewTone(row.release.verdict), row.release.verdict_label)} <span class="mono muted">${fmtTime(row.release.released_at)}</span>`
                  : '<span class="muted">未发布</span>',
            },
            {
              key: 'experiment_id',
              label: '',
              render: (row) =>
                `<button class="btn sm ghost" data-select="${esc(row.experiment_id)}">${row.experiment_id === selectedId ? '当前' : '查看'}</button>`,
            },
          ],
          empty: { title: '还没有实验', hint: '先去实验登记页建实验。' },
        })}
      </section>
      ${
        detail
          ? `<div class="eyebrow" style="margin:4px 0 -6px">当前实验 · ${esc(detail.experiment_code)} ${esc(detail.experiment_name)}</div>
             ${planPanel(detail, ctx, refresh, view)}
             ${runPanel(detail, ctx)}
             ${releasePanel(detail, ctx)}
             ${historyPanel(detail)}`
          : ''
      }
    `;

    view.querySelectorAll('[data-select]').forEach((button) => {
      button.addEventListener('click', () => ctx.navigate(`#/reviews?experiment=${button.dataset.select}`));
    });
    view.querySelector('#edit-plan')?.addEventListener('click', () => openPlanDrawer(view, ctx, detail, refresh));
    view.querySelector('#run-review')?.addEventListener('click', async (event) => {
      const button = event.currentTarget;
      button.disabled = true;
      try {
        const variantSelect = view.querySelector('#run-variant');
        const payload = variantSelect ? { variant_code: variantSelect.value } : {};
        const result = await api.post(`/api/experiments/${detail.experiment_id}/review-runs`, payload);
        toast(result.reused ? '同一批数据，结论未变（未新增记录）' : `评审完成：${result.run.verdict_label}`);
        await refresh();
      } catch (err) {
        toast(errorMessage(err), 'err');
        button.disabled = false;
      }
    });
    view.querySelector('#do-release')?.addEventListener('click', async (event) => {
      const button = event.currentTarget;
      button.disabled = true;
      try {
        const result = await api.post(`/api/experiments/${detail.experiment_id}/review-releases`, {
          run_id: detail.latest_run.id,
        });
        toast(result.switched ? '已发布：判定与依据一次性切换' : '该依据已在线上，无需切换');
        await refresh();
      } catch (err) {
        toast(errorMessage(err), 'err');
        button.disabled = false;
      }
    });
  },
};

export default reviewsPage;
