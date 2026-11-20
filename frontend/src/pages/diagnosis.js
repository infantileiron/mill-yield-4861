import {table} from '../components/common/table.js';
import {badge} from '../components/common/status.js';
import {histogram} from '../components/common/histogram.js';
import {esc,fmt,canEdit} from '../utils/format.js';
import {lines} from '../constants/enums.js';

const indexLabels={empty:'未建立',building:'构建中',active:'正常',lagging:'滞后',failed:'不可用'};
const stamp=v=>v?esc(String(v).slice(0,16).replace('T',' '))+' UTC':'—';

export function indexPanel(s){
  const st=s.indexStatus||{},w=s.indexWindow||{},wm=st.active;
  const lagNote=st.lagging?`<div class="report-note"><strong>索引${esc(indexLabels[st.status]||st.status)}</strong><p>${esc(st.reason||'当前展示上一个完整水位的结果')}。查询与已受理诊断不受影响。</p></div>`:'';
  return `<section class="paper index-panel"><div class="section-head"><h2>滚动出粉率索引</h2><span class="badge ${esc(st.status||'empty')}">${esc(indexLabels[st.status]||'未建立')}</span></div>
  <label class="compact">查看产线<select id="index-line"><option value="" ${s.indexLine?'':'selected'}>全部产线</option>${lines.map(l=>`<option value="${esc(l)}" ${l===s.indexLine?'selected':''}>${esc(l)}</option>`).join('')}</select></label>
  <p class="hint">索引按实际采样时刻收录最近 24 小时的检验。旧记录时间未知，单列统计，不混入窗口。</p>
  <dl class="watermark">
    <div><dt>索引代</dt><dd>${wm?`#${wm.generation_id}`:'—'}</dd></div>
    <div><dt>水位</dt><dd>${wm&&wm.applied_through_id!=null?'id '+wm.applied_through_id:'—'}</dd></div>
    <div><dt>窗口 / 旧记录</dt><dd>${wm?`${wm.window_count??'—'} / ${wm.legacy_count??'—'}`:'—'}</dd></div>
    <div><dt>追平</dt><dd>${st.last_synced_at?stamp(st.last_synced_at):'—'}</dd></div>
    <div><dt>窗口</dt><dd>最近 ${esc(st.window_hours||24)} 小时</dd></div>
  </dl>
  ${lagNote}
  <div class="form-row"><button class="primary" id="index-build" ${canEdit(s.user)?'':'disabled'}>${st.built?'重建索引':'建立索引'}</button><button class="outline" id="index-sync" ${canEdit(s.user)?'':'disabled'}>立即追平</button></div>
  <p class="hint">旧记录是采样时刻未知的历史条目，只在下方单列，不参与窗口分位数。</p>
  <small>首次建立后，新录入与迟到检验自动更新；过期记录按采样时刻移出，不扫描全部历史。</small></section>`;
}

export function windowPanel(s){
  const w=s.indexWindow||{},st=s.indexStatus||{};
  if(!w.built)return `<section><div class="empty">${esc(st.reason||'索引尚未建立，建立后可查询各产线最近 24 小时的精确分位数。')}</div></section>`;
  const rows=Object.entries(w.lines||{}).map(([line,g])=>[
    `<strong>${esc(line)}</strong><small>${g.n?`窗口 ${g.in_window} 条 · 迟到 ${g.late_count} 条`:'窗口内暂无样本'}</small>`,
    g.n?`${fmt(g.q1)} / <strong>${fmt(g.median)}</strong> / ${fmt(g.q3)}`:'—',
    g.n?`${fmt(g.lower)} — ${fmt(g.upper)}`:'—',
    g.n?`${fmt(g.mean)} ± ${fmt(g.std)}`:'—',
    g.n?`<span class="rust">${g.outliers.total}</span>`:'—',
  ]);
  const details=Object.entries(w.lines||{}).filter(([,g])=>g.n).map(([line,g])=>`
    <h3>${esc(line)} · 异常样本 ${g.outliers.total} 条${g.outliers.truncated?'（仅列前 500 条）':''}</h3>
    ${g.outliers.samples.length?table(['样本编号','批次','采样时刻','出粉率','水分','迟到'],g.outliers.samples.map(o=>[
      `<strong>${esc(o.sample_code)}</strong>`,esc(o.batch_code),stamp(o.sampled_at),fmt(o.flour_yield)+'%',fmt(o.moisture)+'%',o.late?'是':'否'
    ])):'<div class="empty">该产线窗口内没有落在 IQR 边界外的样本。</div>'}`);
  const legacy=w.legacy||{};
  return `<section><div class="section-head"><h2>最近 ${esc(w.window?.hours||24)} 小时分布</h2><span>${stamp(w.window?.from)} → ${stamp(w.window?.to)}</span></div>
  <p class="hint">迟到判定：录入时刻比采样时刻晚超过 ${esc(w.window?.late_tolerance_minutes||30)} 分钟。分位数按窗口内全部有效样本线性插值。</p>
  ${table(['产线','Q1 / 中位数 / Q3','复核边界','均值 ± 标准差','异常'],rows)}
  ${details.join('')}
  <h3>旧记录 · 采样时刻未知（${legacy.count||0} 条，未纳入窗口）</h3>
  ${legacy.samples?.length?table(['样本编号','批次','产线','出粉率','水分'],legacy.samples.map(o=>[
    `<strong>${esc(o.sample_code)}</strong>`,esc(o.batch_code),esc(o.line),fmt(o.flour_yield)+'%',fmt(o.moisture)+'%'
  ])):'<div class="empty">没有采样时刻未知的旧记录。</div>'}
  <p class="hint">旧记录不参与滚动分位数；${legacy.truncated?'列表仅展示前 100 条。':'统计与列表口径一致。'}</p></section>`;
}

export function resultPanel(r){
  if(r&&!r.result?.n){
    const pendingText={queued:'已受理，等待后台计算。',running:'后台正在计算，稍后自动刷新。',failed:'计算失败：'+(r.note||'请重新提交')}[r.status]||'尚无结果。';
    return `<div class="section-head"><h2>${esc(r.title)}</h2>${badge(r.status)}</div><div class="empty">${esc(pendingText)}</div>`;
  }
  if(!r)return `<div class="empty">选择一份已完成的诊断，或提交新的产线样本。至少 5 个样本才能计算。</div>`;
  let d=r.result;
  const meta=r.source==='indexed'
    ? `<p class="hint">滚动索引受理 · 代 #${esc(r.index_generation_id)} · 水位 id ${esc(r.index_watermark)} · 窗口 ${esc(r.window_hours)} 小时 · ${stamp(r.window_from)} → ${stamp(r.window_to)}${r.index_lagging?' · <span class="rust">受理时索引滞后</span>':''} · 旧记录 ${esc(r.legacy_count||0)} 条未纳入</p>`
    : `<p class="hint">原诊断口径 · 提交时全量历史快照，后续录入不改写结果。</p>`;
  return `<div class="section-head"><h2>${esc(r.title)}</h2>${badge(r.status)}</div>${meta}
  <div class="metrics"><div><small>样本量</small><b>${d.n}</b></div><div><small>平均出粉率</small><b>${fmt(d.mean)}<em>%</em></b></div><div><small>中位数</small><b>${fmt(d.median)}<em>%</em></b></div><div><small>IQR 异常点</small><b class="rust">${d.outliers.length}</b></div></div>
  ${histogram(d)}<div class="formula">Q1 ${fmt(d.q1)} · Q3 ${fmt(d.q3)} · IQR ${fmt(d.iqr)}<br>复核边界：${fmt(d.lower)}% — ${fmt(d.upper)}% · 样本标准差 ${fmt(d.std)}</div>
  ${table(['产线','样本','均值','最小 / 最大'],Object.entries(d.groups).map(([line,g])=>[esc(line),g.n,fmt(g.mean)+'%',fmt(g.min)+' / '+fmt(g.max)]))}
  <p class="hint">${esc(d.method)}</p>`;
}

export function diagnosisPage(s){
  const selected=s.selected||s.reviews.find(r=>r.result?.n);
  const indexed=s.reviews.filter(r=>r.source==='indexed');
  const legacy=s.reviews.filter(r=>r.source!=='indexed');
  const choice=r=>`<button class="review-choice" data-review="${r.id}"><strong>${esc(r.title)}</strong><small>${esc(r.line)} · #${r.id}${r.source==='indexed'?' · 窗口':' · 历史'}</small>${badge(r.status)}</button>`;
  return `<header><p class="eyebrow">03 / DISTRIBUTION DIAGNOSIS</p><h1>分布诊断</h1><p>滚动索引给出最近 24 小时的准确分位数与异常样本；原全量诊断保留，两者互不改写。</p></header>
  <div class="workspace"><div class="stack">${indexPanel(s)}
  <section class="paper"><h2>受理索引诊断</h2><form id="indexed-review-form"><label>复核标题<input name="title" required minlength="2" value="一线最近24小时出粉率复核"></label><label>产线<select name="line">${lines.map(x=>`<option>${x}</option>`).join('')}</select></label><label>窗口小时<input name="window_hours" type="number" min="1" max="24" value="${esc(s.indexStatus?.window_hours||24)}" required></label><button class="primary" ${canEdit(s.user)&&s.indexStatus?.built?'':'disabled'}>冻结水位并诊断 →</button></form><p class="hint">受理时固定索引代、水位和窗口样本；之后过期、重建都不改写已受理结果。索引未建立或窗口内不足 5 条时拒绝受理。</p>
  <h3>滚动索引诊断</h3><div class="review-list">${indexed.map(choice).join('')||'<div class="empty">还没有索引诊断。</div>'}</div></section></div>
  <div class="stack"><section>${windowPanel(s)}</section>
  <section class="paper"><h2>原全量诊断</h2><form id="review-form"><label>复核标题<input name="title" required minlength="2" value="11月15日产线出粉率复核"></label><label>样本范围<select name="line"><option>全部</option>${lines.map(x=>`<option>${x}</option>`).join('')}</select></label><button class="primary" ${canEdit(s.user)?'':'disabled'}>提交后台诊断 →</button></form><p class="hint">保留原有口径：提交时冻结全部历史检验，后续录入不影响。</p>
  <h3>历史诊断</h3><div class="review-list">${legacy.map(choice).join('')||'<div class="empty">还没有历史诊断。</div>'}</div></section>
  <section id="result-panel">${resultPanel(selected)}</section></div></div>`;
}
