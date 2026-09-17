/* WorkBuddy 用量看板 —— 前端逻辑（无框架、无外部依赖，可离线运行） */
'use strict';

const $ = (s) => document.querySelector(s);
const state = {
  meta: null,
  tab: 'overview',
  sort: { k: 'start', order: 'desc' },
  page: 0,
  limit: 50,
  turnsTotal: 0,
  lastTs: 0,
  liveOk: false,
  dim: 'model',        // 分布维度（每日消耗分布与轮次分布图共用）：model / project / session
  hourDay: '',         // 24 小时分布选中的日期，'' = 全部日期合计
  range: 'all',        // 分布图粒度：all / month / week / day
  rangeKey: '',        // 选中的周期键（月=YYYY-MM / 周=周一日期 / 日=YYYY-MM-DD）
  hourly: null,        // 按小时数据的缓存 { 'YYYY-MM-DD': {...} }
  cfgOpen: false,      // 额度设置表单是否展开
  summary: null,       // 最近一次 /api/summary 结果（切换表单时重绘用）
};

const DIM_LABEL = { model: '模型', project: '项目', session: '会话' };
/* 柱子的配色：刻意避开「积分=橙 / Tokens=蓝」这两个语义色 ——
   那两个颜色留给「当日合计」那条线，它才是重点，不能被柱子抢走。 */
const SERIES_COLORS = ['#0891b2', '#7c3aed', '#059669', '#db2777', '#65a30d'];

/* ---------------------------------------------------------------- 格式化 */
const CN_UNITS = [[1e8, '亿'], [1e4, '万']];

function fmtTokParts(n) {
  n = Number(n) || 0;
  for (const [u, s] of CN_UNITS) {
    if (Math.abs(n) >= u) return { num: (n / u).toFixed(2), unit: s };
  }
  return { num: n.toLocaleString('zh-CN'), unit: '' };
}
function fmtTok(n) { const p = fmtTokParts(n); return p.num + p.unit; }

/* 积分与 Tokens 都拆成「数字 + 单位」，单位用小字号，
   否则 23px 下「亿/万」这类中文字形会显得比数字大一大截，像排版坏了 */
function metricParts(key, v) {
  return key === 'credit' ? { num: fmtCredit(v), unit: '积分' } : fmtTokParts(v);
}
function fmtNum(n) { return (Number(n) || 0).toLocaleString('zh-CN'); }
function fmtCredit(n) {
  const v = Number(n) || 0;
  if (v >= 1000) return v.toFixed(0);
  if (v >= 10) return v.toFixed(1);
  return v.toFixed(2);
}
function fmtDur(ms) {
  if (!ms) return '—';
  const s = Math.round(ms / 1000);
  if (s < 60) return s + 's';
  const m = Math.floor(s / 60), r = s % 60;
  if (m < 60) return m + 'm' + (r ? r + 's' : '');
  return Math.floor(m / 60) + 'h' + (m % 60) + 'm';
}
function fmtTime(ts, withSec) {
  if (!ts) return '—';
  const d = new Date(ts);
  const p = (x) => String(x).padStart(2, '0');
  let s = `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
  if (withSec) s += ':' + p(d.getSeconds());
  return s;
}
function fmtFull(ts) {
  if (!ts) return '—';
  const d = new Date(ts), p = (x) => String(x).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
function oneLine(s, n) {
  const t = String(s || '').replace(/\s+/g, ' ').trim();
  return t.length > n ? t.slice(0, n) + '…' : t;
}

/* ---------------------------------------------------------------- 计量口径
   默认「积分」——这才是真正花钱的口径；Tokens 作为可切换的观察口径。
   切一下 switch，KPI、图表、排行、默认排序、强调列会全部跟着走。 */
const METRICS = {
  credit: {
    key: 'credit',
    label: '积分',
    fmt: fmtCredit,
    axisFmt: (v) => (v >= 100 ? v.toFixed(0) : v.toFixed(1)),
    color: '#ea580c',
    desc: '按积分',
  },
  tokens: {
    key: 'total',
    label: 'Tokens',
    fmt: fmtTok,
    axisFmt: fmtTok,
    color: '#3b82f6',
    desc: '按 Tokens',
  },
};
const METRIC_STORE = 'wb-dashboard-metric';
let metric = 'credit';
try {
  const saved = localStorage.getItem(METRIC_STORE);
  if (saved === 'credit' || saved === 'tokens') metric = saved;
} catch (e) { /* 无痕模式下 localStorage 可能不可用，忽略 */ }
const M = () => METRICS[metric];
const OTHER = () => (metric === 'credit' ? METRICS.tokens : METRICS.credit);

/* ---------------------------------------------------------------- 请求 */
async function api(path, params) {
  const u = new URL(path, location.origin);
  const p = params || filterParams();
  for (const [k, v] of Object.entries(p)) {
    if (v !== '' && v != null) u.searchParams.set(k, v);
  }
  const r = await fetch(u);
  if (!r.ok) throw new Error(path + ' → HTTP ' + r.status);
  return r.json();
}

function filterParams() {
  return {
    from: $('#fFrom').value,
    to: $('#fTo').value,
    project: $('#fProject').value,
    session: $('#fSession').value,
    model: $('#fModel').value,
    scene: $('#fScene').value,
    q: $('#fQ').value.trim(),
  };
}

/* ---------------------------------------------------------------- 图表 */
function drawBars(el, items, opt) {
  opt = opt || {};
  const W = Math.max(el.clientWidth || 620, 280);
  const H = opt.height || 168;
  const padL = opt.padL != null ? opt.padL : 50, padR = 10, padT = 10, padB = 20;
  const n = items.length;
  if (!n) { el.innerHTML = '<p class="note" style="padding:24px 0;text-align:center">当前筛选下没有数据</p>'; return; }
  const max = Math.max(1, ...items.map((d) => d.value));
  const iw = W - padL - padR, ih = H - padT - padB;
  const step = iw / n;
  const bw = Math.max(2, Math.min(opt.maxBar || 48, step * 0.64));
  const fmt = opt.fmt || fmtTok;
  const color = opt.color || '#3b82f6';

  let g = '';
  for (let i = 0; i <= 2; i++) {
    const y = padT + (ih * i) / 2;
    const val = max * (1 - i / 2);
    g += `<line class="axis-line" x1="${padL}" y1="${y.toFixed(1)}" x2="${W - padR}" y2="${y.toFixed(1)}"/>`;
    g += `<text class="axis-label" x="${padL - 7}" y="${(y + 3.5).toFixed(1)}" text-anchor="end">${fmt(val)}</text>`;
  }
  let bars = '';
  items.forEach((d, i) => {
    const h = Math.max(d.value > 0 ? 2 : 0.6, (d.value / max) * ih);
    const x = padL + step * i + (step - bw) / 2;
    const y = padT + ih - h;
    bars += `<rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${bw.toFixed(1)}" height="${h.toFixed(1)}" rx="2" fill="${d.color || color}"><title>${esc(d.title || d.label)}</title></rect>`;
  });
  const every = Math.max(1, Math.ceil(n / 9));
  let labels = '';
  items.forEach((d, i) => {
    if (i % every && i !== n - 1) return;
    labels += `<text class="axis-label" x="${(padL + step * i + step / 2).toFixed(1)}" y="${H - 5}" text-anchor="middle">${esc(d.short != null ? d.short : d.label)}</text>`;
  });
  el.innerHTML = `<svg width="100%" height="${H}" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none"
      style="overflow:visible"><g>${g}${bars}${labels}</g></svg>`;
}

/* 多序列折线图：x 轴是日期，每个实体一条线。
   当前界面已不再使用（分布图固定为柱+线），保留作为通用绘图原语，
   以后若要加只看曲线的视图可直接复用。 */
function drawLines(el, cfg) {
  const days = cfg.days || [];
  const series = (cfg.series || []).filter((s) => s.values && s.values.length);
  const W = Math.max(el.clientWidth || 640, 320);
  const H = cfg.height || 214;
  const padL = 60, padR = 14, padT = 12, padB = 28;
  if (!days.length || !series.length) {
    el.innerHTML = '<p class="note" style="padding:26px 0;text-align:center">当前筛选下没有数据</p>';
    return;
  }
  let max = 1;
  series.forEach((s) => s.values.forEach((v) => { if (v > max) max = v; }));
  const iw = W - padL - padR, ih = H - padT - padB;
  const n = days.length;
  const X = (i) => (n === 1 ? padL + iw / 2 : padL + (iw * i) / (n - 1));
  const Y = (v) => padT + ih * (1 - v / max);
  const fmt = cfg.fmt || fmtTok;

  let g = '';
  for (let i = 0; i <= 2; i++) {
    const y = padT + (ih * i) / 2;
    g += `<line class="axis-line" x1="${padL}" y1="${y.toFixed(1)}" x2="${W - padR}" y2="${y.toFixed(1)}"/>`;
    g += `<text class="axis-label" x="${padL - 9}" y="${(y + 4).toFixed(1)}" text-anchor="end">${esc(fmt(max * (1 - i / 2)))}</text>`;
  }
  let body = '';
  series.forEach((s) => {
    const pts = s.values.map((v, i) => `${X(i).toFixed(1)},${Y(v).toFixed(1)}`).join(' ');
    body += `<polyline points="${pts}" fill="none" stroke="${s.color}" stroke-width="2.4"
      stroke-linejoin="round" stroke-linecap="round"/>`;
    s.values.forEach((v, i) => {
      body += `<circle cx="${X(i).toFixed(1)}" cy="${Y(v).toFixed(1)}" r="3.6" fill="#fff"
        stroke="${s.color}" stroke-width="2"><title>${esc(s.label || s.name)}　${esc(days[i])}　${esc(fmt(v))}</title></circle>`;
    });
  });
  const every = Math.max(1, Math.ceil(n / 9));
  let labels = '';
  days.forEach((d, i) => {
    if (i % every && i !== n - 1) return;
    labels += `<text class="axis-label" x="${X(i).toFixed(1)}" y="${H - 7}" text-anchor="middle">${esc(d.slice(5))}</text>`;
  });
  el.innerHTML = `<svg width="100%" height="${H}" viewBox="0 0 ${W} ${H}"
    preserveAspectRatio="none">${g}${body}${labels}</svg>`;
}

/* 坐标轴刻度：把 0..max 三等分，返回 [{v, y}]（自顶向下）。
   右轴的量纲跟左轴不是一个数量级（积分 ~10²，Tokens ~10⁹），所以两条轴各自独立换算。 */
function axisTicks(max, ih, padT) {
  const out = [];
  for (let i = 0; i <= 2; i++) out.push({ v: max * (1 - i / 2), y: padT + (ih * i) / 2 });
  return out;
}

/* 「积分 + Tokens」双轴折线：画在同一个坐标系里，各有各的纵轴。
   两条线都画，靠**透明度**区分主次：当前选中口径不透明、另一条降到 0.32，
   这样"两个口径同时可见、但一眼看得出现在在看哪个"。
   返回 SVG 片段（被 drawGroupedBars 与 drawBars 复用）。 */
function dualLines(cfg) {
  const lines = (cfg.lines || []).filter((l) => l && l.values && l.values.length);
  if (!lines.length) return '';
  const X = cfg.X;
  const padT = cfg.padT, ih = cfg.ih;
  let svg = '';
  lines.forEach((l) => {
    // 每条线用自己的上限换算 y —— 这就是"双轴"的实质。
    // 上限统一用 cfg.headroom 放大（顶上留白），否则最高点会正好贴在绘图区上沿、
    // 跟顶部轴名撞在一起（踩过）。
    const max = Math.max(1, ...l.values) * (cfg.headroom || 1);
    const dim = l.dim ? 0.32 : 1;                     // 非当前口径 → 降透明
    const pts = l.values.map((v, i) => `${X(i).toFixed(1)},${(padT + ih - (v / max) * ih).toFixed(1)}`).join(' ');
    svg += `<polyline points="${pts}" fill="none" stroke="${l.color}" stroke-width="${dim < 1 ? 2.2 : 3.4}"
      stroke-opacity="${dim}" stroke-linejoin="round" stroke-linecap="round"
      ${l.dash ? `stroke-dasharray="${l.dash}"` : ''}/>`;
    l.values.forEach((v, i) => {
      svg += `<circle cx="${X(i).toFixed(1)}" cy="${(padT + ih - (v / max) * ih).toFixed(1)}"
        r="${dim < 1 ? 3 : 4.6}" fill="#fff" stroke="${l.color}"
        stroke-width="${dim < 1 ? 1.8 : 2.6}" stroke-opacity="${dim}"
        ><title>${esc(l.label)}　${esc(cfg.days[i] != null ? cfg.days[i] : i)}　${esc(l.fmt(v))}</title></circle>`;
    });
  });
  return svg;
}

/* 分组柱状图（分布趋势的"柱状"形态）：每天一组，组内每个实体一根细柱。
   叠加两条折线（积分 / Tokens），左右双轴 —— 两条线各自独立缩放，
   当前口径那条实心、另一条降透明。 */
function drawGroupedBars(el, cfg) {
  const days = cfg.days || [];
  const series = (cfg.series || []).filter((s) => s.values && s.values.length);
  const dual = (cfg.lines || []).filter((l) => l && l.values && l.values.length);
  const W = Math.max(el.clientWidth || 640, 320);
  const H = cfg.height || 214;
  // 右侧要放两样东西，从内到外依次是：口径名 → 右轴刻度。
  // 关键：**口径名必须紧跟自己那条折线的末点**，而末点常把绘图区右边界占满，
  // 所以不能"预留一整栏然后把名字钉在栏首" —— 那样名字与末点之间会裂开
  // 几十像素（用户连着反馈了两次「偏得有点远」）。
  // 改法：名字的 x 直接按**末点圆右缘 + 缝**算，栏宽只作为"不要压到刻度"的下限。
  const NAME_GAP = 18;                     // 末点圆右缘 → 名字左缘的净缝（用户逐步放宽：7 → 12 → 18）
  const TICK_W = 58;                       // 右轴刻度栏
  // 名字栏按最宽的口径名估宽（"Tokens" 6 字 @15px 粗体 ≈ 55px），
  // 再加名字右缘与刻度栏之间的呼吸位 —— 否则名字尾部会压到刻度上（踩过）。
  // ⚠️ 字号跟着模式变（当前口径更大，见 .axis-name.on），所以按**最大的那档**算。
  const nameW = dual.length
    ? Math.max(28, Math.round(Math.max(...dual.map((l) => String(l.label || '').length)) * 9.2))
    : 0;
  const NAME_W = nameW + Math.max(11, NAME_GAP + 4);
  const padR = dual.length ? NAME_W + TICK_W : 14;
  const padL = 60, padT = dual.length ? 18 : 12;
  const padB = dual.length ? 32 : 28;
  // 顶上留 8% 余量：折线的最高点不能贴着上沿
  const HEAD = 1.08;
  if (!days.length || !series.length) {
    el.innerHTML = '<p class="note" style="padding:26px 0;text-align:center">当前筛选下没有数据</p>';
    return;
  }
  let max = 1;
  series.forEach((s) => s.values.forEach((v) => { if (v > max) max = v; }));
  // 柱子也要按同一个余量缩放，否则柱子会比折线"显得更高"，两个口径看起来不一致
  max *= HEAD;
  const iw = W - padL - padR, ih = H - padT - padB;
  const n = days.length, k = series.length;
  const cluster = iw / n;
  const gap = 2.5;
  const barW = Math.max(2, Math.min(26, (cluster * 0.74 - gap * (k - 1)) / k));
  const fmt = cfg.fmt || fmtTok;
  const X = (i) => padL + cluster * i + cluster / 2;

  let g = '';
  axisTicks(max, ih, padT).forEach((t) => {
    g += `<line class="axis-line" x1="${padL}" y1="${t.y.toFixed(1)}" x2="${(W - padR).toFixed(1)}" y2="${t.y.toFixed(1)}"/>`;
    g += `<text class="axis-label" x="${padL - 9}" y="${(t.y + 4).toFixed(1)}" text-anchor="end">${esc(fmt(t.v))}</text>`;
  });
  let bars = '';
  days.forEach((day, di) => {
    const groupW = barW * k + gap * (k - 1);
    const x0 = padL + cluster * di + (cluster - groupW) / 2;
    series.forEach((s, si) => {
      const v = s.values[di] || 0;
      const h = Math.max(v > 0 ? 2 : 0.6, (v / max) * ih);
      const x = x0 + si * (barW + gap);
      bars += `<rect x="${x.toFixed(1)}" y="${(padT + ih - h).toFixed(1)}" width="${barW.toFixed(1)}"
        height="${h.toFixed(1)}" rx="1.5" fill="${s.color}"><title>${esc(s.label || s.name)}　${esc(day)}　${esc(fmt(v))}</title></rect>`;
    });
  });
  // 双折线（积分 / Tokens），各自一个纵轴
  bars += dualLines({ lines: dual, X, padT, ih, days, headroom: HEAD });

  // 双轴：**左轴 = 当前口径（柱子的口径）**，右轴 = 另一个口径。
  // 关键点是「轴归属稳定」：切开关时左轴始终服务柱子、右轴始终服务那条对照线，
  // 所以左轴刻度用 cfg.fmt（跟着柱子走）、右轴刻度用对照线自己的 fmt ——
  // 曾经把左轴写死成积分格式，结果 Tokens 模式下把 1356 积分 打成「13.56亿」（踩过）。
  if (dual.length) {
    const R = dual.filter((l) => l.axis === 'right')[0];
    const L = dual.filter((l) => l.axis === 'left')[0];
    // 两条右栏的位置：绘图区右边界 → 口径名 → 右轴刻度（最外）
    const plotRight = W - padR;
    const tickX = (plotRight + NAME_W).toFixed(1);
    // 右轴刻度：用对照线自己的格式（那是它自己的量纲）
    if (R) {
      const rmax = Math.max(1, ...R.values) * HEAD;
      axisTicks(rmax, ih, padT).forEach((t) => {
        bars += `<text class="axis-label" x="${tickX}" y="${(t.y + 4).toFixed(1)}"
          fill="${R.color}" text-anchor="start">${esc(R.fmt(t.v))}</text>`;
      });
    }
    // 口径名：**直接捕捉各自那条折线末点的位置**，紧贴末点右侧、同一水平线。
    // 两层约束：
    //   下界 = 末点圆右缘 + NAME_GAP  → 不骑在点上
    //   上界 = 绘图区右边界 + NAME_FLOOR 之前必须收住 → 不压右轴刻度
    // 末点若已顶到绘图区右边界，名字就落在边界外侧的留白里，
    // 这正好是"最后一个点的后面"；若末点靠左（数据少/窄屏），名字就跟过去，
    // 而不是死钉在栏首 —— 这是"看上去离得远"的根因。
    [L, R].forEach((ln) => {
      if (!ln || !ln.values || !ln.values.length) return;
      // 与 dualLines 用**同一个** max/HEAD 换算末点 y，保证名字正好落在末点旁
      const lmax = Math.max(1, ...ln.values) * HEAD;
      const lastV = ln.values[ln.values.length - 1] || 0;
      const y = padT + ih - (lastV / lmax) * ih;
      // 末点贴上下沿时把文字收进绘图区，避免飘出去
      const ty = Math.min(padT + ih, Math.max(padT, y)) + 4.5;
      // 末点圆的右缘（dim=false → r=4.6；dim=true → r=3），再留 NAME_GAP 的缝
      const dotR = ln.dim ? 3 : 4.6;
      const dotX = X(ln.values.length - 1);
      const nx = dotX + dotR + NAME_GAP;
      // 字体跟着模式变：当前口径（跟柱子同侧那条）加粗放大，另一口径缩小淡化 ——
      // 与折线的"实心 / 淡出"主次保持一致，扫一眼就知道现在在看哪个口径。
      bars += `<text class="axis-name${ln.dim ? ' dim' : ' on'}" x="${nx.toFixed(1)}" y="${ty.toFixed(1)}"
        fill="${ln.color}" text-anchor="start">${esc(ln.label)}</text>`;
    });
  }
  const xf = cfg.xFmt || ((d) => d.slice(5));
  const every = Math.max(1, Math.ceil(n / 10));
  let labels = '';
  days.forEach((d, i) => {
    if (i % every && i !== n - 1) return;
    labels += `<text class="axis-label" x="${X(i).toFixed(1)}" y="${H - 7}" text-anchor="middle">${esc(xf(d))}</text>`;
  });
  el.innerHTML = `<svg width="100%" height="${H}" viewBox="0 0 ${W} ${H}"
    preserveAspectRatio="none">${g}${bars}${labels}</svg>`;
}

function drawRank(el, rows, opt) {
  opt = opt || {};
  if (!rows.length) { el.innerHTML = '<p class="note">无数据</p>'; return; }
  const m = M();
  const max = Math.max(1, ...rows.map((r) => Number(r.value) || 0));
  el.innerHTML = rows.map((r) => {
    const pct = ((Number(r.value) || 0) / max) * 100;
    const right = r.rightHtml != null ? r.rightHtml
      : (r.right != null ? esc(r.right) : esc(m.fmt(r.value)));
    return `<div class="rankrow">
      <span class="nm" title="${esc(r.label)}${r.title ? '　' + esc(r.title) : ''}">${esc(r.label)}${r.sub ? `<span class="sub2">${esc(r.sub)}</span>` : ''}</span>
      <span class="vv">${right}</span>
      <span class="ranktrack${m === METRICS.credit ? ' amber' : ''}"><i style="width:${pct.toFixed(2)}%"></i></span>
    </div>`;
  }).join('');
}

/* ---------------------------------------------------------------- 头部 */
/* 本月/本周期额度条：账户级数字，**不随筛选变化**，所以单独放在筛选区之上。
   「设置」展开成表单，填完保存到 config.local.json 的 quota 段（服务端），之后自动滚动计算剩余。 */
const CFG_TIP = '「当前剩余」填你去官方用量页看到的那个数字。保存后看板以这一刻为基准，'
  + '自动减去之后新增的消耗；到了刷新日再来更新一次即可。';

function renderMonth(s) {
  const box = $('#monthBar');
  const mb = s && s.month;
  if (!mb) { box.classList.add('hide'); return; }
  box.classList.remove('hide');
  if (state.cfgOpen) { renderConfigForm(mb); return; }

  const m = M();
  const hasPlan = mb.plan != null;
  const hasRemain = mb.remaining != null;
  const pct = mb.usedRatio != null ? Math.min(100, Math.max(0, mb.usedRatio * 100)) : 0;
  const low = hasRemain && mb.plan > 0 && mb.remaining <= mb.plan * 0.2;
  const sameMonth = mb.cycleStart.slice(0, 7) === mb.cycleEnd.slice(0, 7);

  const parts = [];
  parts.push(`<span class="mb-tag">${sameMonth ? esc(mb.cycleStart.slice(0, 7)) : '周期'}</span>`);
  parts.push(`<span class="mb-item">本周期（${esc(mb.cycleStart.slice(5))} ~ ${esc(mb.cycleEnd.slice(5))}）已消耗`
    + `<b>${esc(fmtCredit(mb.used))}</b>积分</span>`);

  if (hasPlan && hasRemain) {
    parts.push(`<span class="mb-item ${low ? 'warn' : ''}">剩余<b>${esc(fmtCredit(mb.remaining))}</b>`
      + `/ ${esc(fmtCredit(mb.plan))} 积分</span>`);
    parts.push(`<span class="mb-progress" title="已用 ${pct.toFixed(1)}%"><i style="width:${pct.toFixed(1)}%"></i></span>`);
    if (mb.perDayLeft != null) {
      parts.push(`<span class="mb-item">周期内还剩 ${mb.daysLeft} 天 · 日均可用`
        + `<b>${esc(fmtCredit(mb.perDayLeft))}</b></span>`);
    }
  } else if (hasPlan) {
    parts.push(`<span class="mb-item">套餐额度 <b>${esc(fmtCredit(mb.plan))}</b>积分</span>`);
  } else {
    parts.push('<span class="mb-item">额度<b>未设置</b></span>');
  }
  parts.push(`<button id="cfgOpenBtn" class="ghost mb-set">${mb.configured ? '设置' : '去设置'}</button>`);

  let hint;
  if (mb.stale) {
    hint = `⚠️ 上次填的剩余量已经跨过刷新日（${esc(mb.cycleNext)} 就是下一次），不再准确 ——`
      + ` 点上面的「设置」重新填一次当前剩余，或只填套餐额度让我按额度推算。`;
  } else if (mb.source === 'snapshot') {
    const sn = mb.snapshot || {};
    // 本机记录的消耗 与「额度−剩余」隐含的消耗 常常对不上（别处的消耗、或计费口径差异）。
    // 说明白比装作一致更有用 —— 剩余一律以用户填的数为准。
    const implied = mb.plan != null ? mb.plan - (sn.remain || 0) : null;
    const off = (implied != null && mb.plan > 0 && Math.abs(implied - mb.used) > mb.plan * 0.02)
      ? `（本机记录本周期消耗 ${esc(fmtCredit(mb.used))}，与"额度−剩余"隐含的 `
        + `${esc(fmtCredit(implied))} 有差，剩余一律以你填的为准）`
      : '';
    hint = `口径：以你填的剩余量 <b>${esc(fmtCredit(sn.remain || 0))}</b> 为基准，`
      + `减去之后的消耗 <b>${esc(fmtCredit(sn.usedSince || 0))}</b>，自动滚动计算。${off}`
      + ` 下次刷新 ${esc(mb.cycleNext)}，到时候来更新一次剩余量即可。`;
  } else if (mb.source === 'plan') {
    hint = `口径：套餐额度 − 本周期已消耗。想更准的话，在「设置」里填一次`
      + `「当前剩余」（官方用量页上的数字），之后我按它滚动扣减。`;
  } else {
    hint = '本地会话记录里只有「每次请求花了多少积分」，<b>没有账户余额</b>。'
      + '点「去设置」填上套餐额度和当前剩余，就能看到剩余与日均可用。';
  }
  parts.push(`<div class="mb-hint">ⓘ ${hint} 官方权威余额见 `
    + `<a href="${esc(mb.usagePage)}" target="_blank" rel="noopener">官方用量页 ↗</a></div>`);
  box.innerHTML = parts.join('');
  $('#cfgOpenBtn').addEventListener('click', () => {
    state.cfgOpen = true;
    renderMonth(state.summary);
  });
}

function renderConfigForm(mb) {
  const box = $('#monthBar');
  const plan = mb.plan != null ? String(Math.round(mb.plan * 100) / 100) : '';
  // 预填"当前剩余"用**算出来的剩余**：这样只改刷新日再保存，也不会把基准点算错
  const remain = mb.remaining != null ? String(Math.round(mb.remaining * 100) / 100) : '';
  box.innerHTML = `
    <div class="cfgform">
      <label><span>每月套餐额度</span>
        <input id="cfgPlan" type="number" min="0" step="1" value="${esc(plan)}" placeholder="如 10000"><em>积分</em></label>
      <label><span>当前剩余</span>
        <input id="cfgRemain" type="number" min="0" step="0.01" value="${esc(remain)}" placeholder="官方用量页上的数"><em>积分</em></label>
      <label><span>每月刷新日</span>
        <input id="cfgReset" type="number" min="1" max="31" value="${esc(String(mb.resetDay || 1))}"><em>号</em></label>
      <button id="cfgSave" class="primary">保存</button>
      <button id="cfgCancel" class="ghost">取消</button>
      <span id="cfgMsg" class="cfgmsg"></span>
      <div class="mb-hint">ⓘ ${CFG_TIP} 留空表示不设该项（只填套餐额度也可以，我按「额度 − 本周期已消耗」推算）。
        数据存在看板目录的 <code>config.local.json</code>，只在本机（该文件已被 .gitignore 排除，不会进仓库）。</div>
    </div>`;
  $('#cfgCancel').addEventListener('click', () => {
    state.cfgOpen = false;
    renderMonth(state.summary);
  });
  $('#cfgSave').addEventListener('click', saveConfig);
}

async function saveConfig() {
  const msg = $('#cfgMsg');
  const readNum = (id) => {
    const v = $(id).value.trim();
    return v === '' ? null : Number(v);
  };
  const body = { plan: readNum('#cfgPlan'), remaining: readNum('#cfgRemain'), resetDay: readNum('#cfgReset') };
  if (body.plan != null && !(body.plan >= 0)) { msg.textContent = '套餐额度要填非负数'; return; }
  if (body.remaining != null && !(body.remaining >= 0)) { msg.textContent = '当前剩余要填非负数'; return; }
  msg.textContent = '保存中…';
  try {
    const r = await fetch('/api/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const d = await r.json();
    if (!d.ok) throw new Error(d.error || '服务端拒绝');
    state.cfgOpen = false;
    toast('额度设置已保存');
    await refresh();
  } catch (e) {
    msg.textContent = '保存失败：' + e.message;
  }
}

function renderIdents() {
  const m = state.meta, box = $('#idents');
  const acc = (m.identity.accounts || [])[0];
  const dev = (m.identity.devices || [])[0];
  let html = '';
  if (acc) {
    html += `<div class="badge" title="账号标识 ${esc(acc.id)}（工作区无邮箱/昵称落盘，用 UUID + 类型表示）">
      <span class="k">账号</span><b>${esc(acc.label)}</b></div>`;
  } else {
    html += `<div class="badge"><span class="k">账号</span><b>未识别</b></div>`;
  }
  if (dev) {
    html += `<div class="badge" title="设备 ${esc(dev.id)} · ${esc(dev.os)}">
      <span class="k">设备</span><b>${esc(dev.hostname)}</b><span class="v">${esc(dev.id.slice(0, 8))}</span></div>`;
    html += `<div class="badge"><span class="k">客户端</span><b>v${esc(dev.version)}</b></div>`;
  }
  html += `<div class="badge"><span class="k">数据</span><b>${fmtNum(m.counts.requests)}</b>
    <span class="v">请求 / ${m.counts.sessions} 会话 / ${m.counts.files} 文件</span></div>`;
  box.innerHTML = html;
  $('#attrNote').textContent = 'ⓘ ' + m.attribution_note;
  $('#footDir').textContent = m.projectsDir;
}

/* ---------------------------------------------------------------- KPI */
/* 层级：1 张「主角卡」承载当前口径的主指标 + 另一口径对照，下面才是支撑性小卡。
   数字与单位分开渲染（数字大、单位小），副值独占一行 —— 既好看也不会被拆行。 */
function bigValue(parts, cls) {
  const unit = parts.unit ? `<span class="unit">${esc(parts.unit)}</span>` : '';
  return `<div class="v ${cls || ''}"><span class="num">${esc(parts.num)}</span>${unit}</div>`;
}

function renderKpis(s) {
  const t = s.totals;
  const turns = t.user_turns || 0;
  const pct = (s.cache_hit_rate * 100).toFixed(1) + '%';
  const isCredit = metric === 'credit';
  const main = M();
  const side = OTHER();

  const mainParts = metricParts(main.key, t[main.key]);
  const sideParts = metricParts(side.key, t[side.key]);
  const perTurn = metricParts(main.key, turns ? t[main.key] / turns : 0);
  const perReq = metricParts(main.key, t.req ? t[main.key] / t.req : 0);
  // 标签已经写明口径时就不重复单位（避免出现「积分 5638 积分」）
  const sideLabel = side.label === '积分' ? '积分消耗' : side.label;
  const sideUnit = sideParts.unit === side.label ? '' : sideParts.unit;

  const hero = `
    <div class="kpi hero ${isCredit ? 'is-credit' : 'is-tokens'}">
      <div class="block main">
        <div class="k">${isCredit ? '积分消耗' : '合计 Tokens'}</div>
        ${bigValue(mainParts, 'xl')}
        <div class="note">平均每轮 ${esc(perTurn.num + perTurn.unit)} · 平均每请求 ${esc(perReq.num + perReq.unit)}</div>
      </div>
      <div class="divider"></div>
      <div class="block side">
        <div class="k">${esc(sideLabel)}</div>
        ${bigValue({ num: sideParts.num, unit: sideUnit }, 'lg')}
        <div class="note">缓存命中 ${esc(pct)}</div>
      </div>
      <div class="divider"></div>
      <div class="block side">
        <div class="k">提问轮次 / 会话 / 请求</div>
        <div class="v lg"><span class="num">${fmtNum(turns)}</span><span class="unit">轮</span></div>
        <div class="note">${fmtNum(t.sessions)} 个会话 · ${fmtNum(t.req)} 次请求</div>
      </div>
    </div>`;

  const card = (k, parts, sub) => `
    <div class="kpi">
      <div class="k">${esc(k)}</div>
      ${bigValue(parts)}
      ${sub ? `<small>${esc(sub)}</small>` : ''}
    </div>`;

  const cards = isCredit
    ? [
      card('缓存命中率', { num: pct, unit: '' }, `命中省下 ${fmtTok(t.cached)}`),
      card('输入 Tokens', fmtTokParts(t.inp), fmtNum(t.inp)),
      card('输出 Tokens', fmtTokParts(t.out), fmtNum(t.out)),
      card('思考 Tokens', fmtTokParts(t.reasoning), fmtNum(t.reasoning)),
      card('平均每轮 Tokens', fmtTokParts(turns ? t.total / turns : 0), `共 ${fmtNum(turns)} 轮`),
    ]
    : [
      card('缓存命中率', { num: pct, unit: '' }, `命中省下 ${fmtTok(t.cached)}`),
      card('输入 Tokens', fmtTokParts(t.inp), fmtNum(t.inp)),
      card('输出 Tokens', fmtTokParts(t.out), fmtNum(t.out)),
      card('思考 Tokens', fmtTokParts(t.reasoning), fmtNum(t.reasoning)),
      card('积分消耗', metricParts('credit', t.credit), '实际计费口径'),
    ];

  $('#kpis').innerHTML = hero + cards.join('');
}

/* ---- 范围粒度：全部 / 按月 / 按周 / 按日（下拉共用一个位置，选项随粒度变） ---- */
/* 周以「周一」为起点 */
function weekOf(dayStr) {
  const d = new Date(dayStr + 'T00:00:00');
  const dow = (d.getDay() + 6) % 7;          // 周一=0
  const mon = new Date(d); mon.setDate(d.getDate() - dow);
  const sun = new Date(mon); sun.setDate(mon.getDate() + 6);
  const f = (x) => {
    const p = (n) => String(n).padStart(2, '0');
    return `${x.getFullYear()}-${p(x.getMonth() + 1)}-${p(x.getDate())}`;
  };
  return { key: f(mon), start: f(mon), end: f(sun), label: `${f(mon).slice(5)} ~ ${f(sun).slice(5)}` };
}

/* 当前粒度下可供选择的周期（新的在前） */
function rangeOptions(days) {
  if (state.range === 'month') {
    const ms = [...new Set(days.map((d) => d.slice(0, 7)))].sort().reverse();
    return ms.map((m) => ({ key: m, label: m, test: (d) => d.slice(0, 7) === m }));
  }
  if (state.range === 'week') {
    const seen = {};
    days.forEach((d) => { const w = weekOf(d); seen[w.key] = w; });
    return Object.keys(seen).sort().reverse()
      .map((k) => ({ key: k, label: seen[k].label, test: (d) => d >= seen[k].start && d <= seen[k].end }));
  }
  if (state.range === 'day') {
    return [...days].reverse().map((d) => ({ key: d, label: d, test: (x) => x === d }));
  }
  return [];
}

/* 下拉共享一个位置：按当前粒度填选项，选中的周期失效时回落到最新一个 */
function fillRangePick(days) {
  const sel = $('#rangePick');
  const opts = rangeOptions(days);
  if (!opts.length) { sel.classList.add('hide'); sel.innerHTML = ''; return; }
  const keys = opts.map((o) => o.key);
  if (keys.indexOf(state.rangeKey) < 0) state.rangeKey = keys[0];
  sel.innerHTML = opts.map((o) =>
    `<option value="${esc(o.key)}"${o.key === state.rangeKey ? ' selected' : ''}>${esc(o.label)}</option>`).join('');
  sel.classList.remove('hide');
}

async function loadHourly(day) {
  if (!state.hourly) state.hourly = {};
  if (!state.hourly[day]) state.hourly[day] = await api('/api/hourly', { day });
  return state.hourly[day];
}

/* ---------------------------------------------------------------- 总览 */
/* 每日消耗分布（唯一的分布图）：柱 = 按当前维度的各实体，线 = 合计。
   口径跟随顶部「积分 / Tokens」开关走；粒度由 全部/按月/按周/按日 控制。 */
async function renderSeries(s) {
  const m = M();
  const days = (s.series && s.series.days) || [];
  const all = (s.series && s.series.dims && s.series.dims[state.dim]) || [];
  fillRangePick(days);

  // 决定横轴：按日 → 24 小时；其他 → 该范围内的每一天
  let labels; let rows;
  if (state.range === 'day' && state.rangeKey) {
    const h = await loadHourly(state.rangeKey);
    labels = h.labels;
    rows = (h.dims && h.dims[state.dim]) || [];
  } else {
    const opt = rangeOptions(days).find((o) => o.key === state.rangeKey);
    const keep = days.map((d, i) => i).filter((i) => !opt || opt.test(days[i]));
    labels = keep.map((i) => days[i]);
    rows = all.map((r) => ({
      ...r,
      values_credit: keep.map((i) => (r.values_credit || [])[i] || 0),
      values_total: keep.map((i) => (r.values_total || [])[i] || 0),
      values_req: keep.map((i) => (r.values_req || [])[i] || 0),
    }));
  }

  const sorted = [...rows].sort((a, b) => b[m.key] - a[m.key]);
  const top = sorted.slice(0, 5);
  const unit = state.range === 'day' ? '每小时' : '每日';
  // 标题里的"每日"要跟着粒度变：横轴是天就说每日，下钻到某一天就说每小时
  const titlePrefix = { all: '每日', month: '该月每日', week: '该周每日', day: '该日每小时' }[state.range]
    || '每日';
  $('#seriesTitle').textContent = `${titlePrefix}消耗分布`;
  const scope = state.range === 'all' ? ''
    : `　范围＝${(rangeOptions(days).find((o) => o.key === state.rangeKey) || {}).label || state.rangeKey}`;
  const oth = OTHER();
  $('#seriesHint').textContent =
    `柱＝按${DIM_LABEL[state.dim]}分布的${unit}${m.label}（${top.length}/${sorted.length} 个）`
    + `　线＝每条${state.range === 'day' ? '每小时' : '每天'}合计：`
    + `${m.label}（左轴·实心）＋ ${oth.label}（右轴·淡）${scope}`;

  const series = top.map((r, i) => ({
    name: r.name,
    key: r.key,
    label: r.name,
    color: SERIES_COLORS[i % SERIES_COLORS.length],
    values: r['values_' + m.key] || [],
    // 两个口径的逐点序列都留着：柱子只画当前口径，但两条折线要用各自的
    vals: { credit: r.values_credit || [], total: r.values_total || [] },
    total: r[m.key],
  }));
  // 展示名可能撞车（例如 hy4-preview 与 hy4-preview-f 都叫 "Hy4 preview"）。
  // 撞车时单独挂一个后缀标识 —— 注意不能直接拼进 name 再整体截断，
  // 那样后缀会被截掉、两条线在图例里还是分不清（踩过）。
  const seen = {};
  series.forEach((x) => { seen[x.name] = (seen[x.name] || 0) + 1; });
  series.forEach((x) => {
    if (seen[x.name] > 1) {
      x.dis = `(${String(x.key).slice(0, 16)})`;
      x.label = `${x.name} ${x.dis}`;
    }
  });

  const useCredit = metric === 'credit';
  // 两条折线：逐点合计，两个口径各一条。左右轴各自独立缩放 ——
  // 积分(百)与 Tokens(亿) 量纲差 5~6 个数量级，共用一轴会把积分压成贴着 0 的直线。
  const lineCredit = labels.map((_, i) => series.reduce((a, x) => a + ((x.vals.credit || [])[i] || 0), 0));
  const lineTok = labels.map((_, i) => series.reduce((a, x) => a + ((x.vals.total || [])[i] || 0), 0));
  // **左轴永远服务柱子和当前口径那条线，右轴服务另一口径** —— 这样轴归属稳定：
  // 切开关时只有"谁的线实心、谁的刻度变主色"变化，轴的位置和含义不变。
  const dual = useCredit
    ? [
      { axis: 'left', color: METRICS.credit.color, fmt: fmtCredit, label: METRICS.credit.label,
        values: lineCredit, dim: false },
      { axis: 'right', color: METRICS.tokens.color, fmt: fmtTok, label: METRICS.tokens.label,
        values: lineTok, dim: true },
    ]
    : [
      { axis: 'left', color: METRICS.tokens.color, fmt: fmtTok, label: METRICS.tokens.label,
        values: lineTok, dim: false },
      { axis: 'right', color: METRICS.credit.color, fmt: fmtCredit, label: METRICS.credit.label,
        values: lineCredit, dim: true },
    ];
  drawGroupedBars($('#seriesChart'), {
    days: labels, series, fmt: m.axisFmt, height: 250,
    lines: dual,
    xFmt: state.range === 'day' ? ((d) => d) : ((d) => d.slice(5)),
  });

  // 时段合计：两个口径都报，当前口径实心加粗、另一口径淡显（和图里两条线的明暗一致）
  const grandCredit = lineCredit.reduce((a, b) => a + b, 0) || 0;
  const grandTok = lineTok.reduce((a, b) => a + b, 0) || 0;
  const totalLabel = { all: '所选范围合计', month: '该月合计', week: '该周合计', day: '该日合计' }[state.range]
    || '所选范围合计';
  const cell = (k, v, on) => `<i style="background:${k.color};opacity:${on ? 1 : 0.32}"></i>`
    + `<span style="opacity:${on ? 1 : 0.62}">${esc(k.label)}</span>`
    + `<b style="color:${k.color};opacity:${on ? 1 : 0.6}">${esc(k.fmt(v))}</b>`;
  $('#seriesTotal').innerHTML = `<span>${esc(totalLabel)}</span>`
    + cell(METRICS.credit, grandCredit, metric === 'credit')
    + cell(METRICS.tokens, grandTok, metric === 'tokens');

  // 图例只列各实体（合计那行已经在图上方单独显示了）
  const base = grandCredit || 1;
  $('#seriesLegend').innerHTML = series.length
    ? series.map((x) => `<span class="lg" title="${esc(x.label)}">`
        + `<i style="background:${x.color}"></i>${esc(oneLine(x.name, x.dis ? 13 : 22))}`
        + (x.dis ? `<span class="sub2">${esc(x.dis)}</span>` : '')
        + `<b>${esc(m.fmt(x.total))}</b>`
        + `<span class="sub2">${(x.total / base * 100).toFixed(0)}%</span></span>`).join('')
    : '';
}

/* 24 小时分布：可以只看某一天 */
function fillHourDay(s) {
  const sel = $('#hourDay');
  const days = (s.hours_by_day || []).map((x) => x.day).slice().reverse();
  sel.innerHTML = '<option value="">全部（合计）</option>'
    + days.map((d) => `<option value="${esc(d)}">${esc(d)}</option>`).join('');
  sel.value = days.includes(state.hourDay) ? state.hourDay : '';
  state.hourDay = sel.value;
}

function renderHourChart(s) {
  const m = M();
  const day = state.hourDay;
  let hours = s.by_hour || [];
  if (day) {
    const rec = (s.hours_by_day || []).find((x) => x.day === day);
    hours = rec ? rec.hours : [];
    $('#hourHint').textContent = `${day} 各小时（${m.label}）`;
  } else {
    $('#hourHint').textContent = `全部日期合计（${m.label}）`;
  }
  const box = $('#hourChart');
  const items = hours.map((h) => ({
    label: h.hour, short: h.hour, value: h[m.key],
    title: `${day || '全部日期'} ${h.hour}:00–${h.hour}:59　${fmtNum(h.req)} 次请求　`
      + `积分 ${h.credit.toFixed(2)}　Tokens ${fmtNum(h.total)}`,
  }));
  const draw = (h) => drawBars(box, items,
    { height: h, color: m.color, padL: 50, fmt: m.axisFmt });
  // 这张卡是 flex 撑满整行高度的，图要跟着卡片长高，否则下方会留一大片空白。
  // 首帧量到的 flex 高度可能还没算好，下一帧再量一次补画。
  let h = Math.round(box.clientHeight);
  if (!h || h < 150) h = 150;
  draw(h);
  requestAnimationFrame(() => {
    const h2 = Math.round(box.clientHeight);
    if (h2 >= 150 && Math.abs(h2 - h) > 4) draw(h2);
  });
}

async function renderOverview(s) {
  const m = M();
  const other = OTHER();

  // 唯一的分布图：柱＝按当前维度的各实体，线＝合计；口径跟随顶部开关
  // ⚠️ 这里是 await，不能写成 return —— 一 return 下面所有卡片（24 小时分布、三个排行）
  //    全成了死代码，页面只剩下标题（踩过，被用户当场发现）
  await renderSeries(s);
  fillHourDay(s);
  renderHourChart(s);

  ['#rankProjectHint', '#rankSessionHint', '#rankModelHint']
    .forEach((sel) => { $(sel).textContent = m.desc; });

  const topBy = (rows, n) => [...rows].sort((a, b) => b[m.key] - a[m.key]).slice(0, n);
  const withOther = (r) => `${other.label} ${other.fmt(r[other.key])}`;
  const grand = s.totals[m.key] || 1;
  const pctOf = (v) => `<span class="pc">${((v || 0) / grand * 100).toFixed(1)}%</span>`;
  const rightOf = (r) => m.fmt(r[m.key]) + pctOf(r[m.key]);

  drawRank($('#rankProject'), topBy(s.by_project || [], 8).map((r) => ({
    label: r.name, sub: withOther(r), value: r[m.key], title: r.key, rightHtml: rightOf(r),
  })));
  drawRank($('#rankSession'), topBy(s.by_session || [], 8).map((r) => ({
    label: r.title || '(无标题会话)', sub: withOther(r), value: r[m.key], title: r.key,
    rightHtml: rightOf(r),
  })));
  drawRank($('#rankModel'), topBy(s.by_model || [], 8).map((r) => ({
    label: r.name, sub: withOther(r), value: r[m.key], title: r.key, rightHtml: rightOf(r),
  })));
}

/* ---------------------------------------------------------------- 轮次分布图
   轮次明细页的图表形态：每一轮一个柱/点，按 模型/项目/会话 着色。
   （表格是"明细"，这张图是"分布" —— 两个都要。） */
function turnDimKey(r) {
  if (state.dim === 'project') return r.project || '(未归类)';
  if (state.dim === 'session') return r.session;
  return (r.models && r.models[0]) || '(未知模型)';
}
function turnDimName(r) {
  if (state.dim === 'project') return r.project || '(未归类)';
  if (state.dim === 'session') return r.sessionTitle || String(r.session).slice(0, 8);
  return (r.modelNames && r.modelNames[0]) || turnDimKey(r);
}

function drawTurnChart(el, rows, opts) {
  const m = M();
  const W = Math.max(el.clientWidth || 640, 320);
  const H = opts.height || 200;
  const padL = 62, padR = 12, padT = 12, padB = 26;
  if (!rows.length) {
    el.innerHTML = '<p class="note" style="padding:30px 0;text-align:center">当前筛选下没有轮次</p>';
    return;
  }
  const vals = rows.map((r) => Number(r[m.key]) || 0);
  const max = Math.max(1, ...vals);
  const ih = H - padT - padB, iw = W - padL - padR;
  const n = rows.length;
  const step = iw / n;
  const bw = Math.max(1.2, Math.min(26, step * 0.82));

  let g = '';
  for (let i = 0; i <= 2; i++) {
    const y = padT + (ih * i) / 2;
    g += `<line class="axis-line" x1="${padL}" y1="${y.toFixed(1)}" x2="${W - padR}" y2="${y.toFixed(1)}"/>`;
    g += `<text class="axis-label" x="${padL - 9}" y="${(y + 4).toFixed(1)}" text-anchor="end">${esc(m.axisFmt(max * (1 - i / 2)))}</text>`;
  }

  let body = '';
  const cx = (i) => padL + step * i + step / 2;
  const cy = (v) => padT + ih - (v / max) * ih;
  if (opts.form === 'line') {
    body += `<polyline points="${rows.map((r, i) => `${cx(i).toFixed(1)},${cy(vals[i]).toFixed(1)}`).join(' ')}"
      fill="none" stroke="${m.color}" stroke-width="1.8" stroke-linejoin="round"/>`;
  }
  rows.forEach((r, i) => {
    const v = vals[i];
    const title = `第 ${i + 1} 轮　${fmtTime(r.start)}　${m.label} ${m.fmt(v)}　`
      + `${fmtNum(r.req)} 次请求　${oneLine(r.prompt, 56)}`;
    if (opts.form === 'line') {
      body += `<circle cx="${cx(i).toFixed(1)}" cy="${cy(v).toFixed(1)}" r="2.2"
        fill="${opts.colorOf(r)}" stroke="#fff" stroke-width="0.8"><title>${esc(title)}</title></circle>`;
    } else {
      const h = Math.max(v > 0 ? 1.5 : 0.5, (v / max) * ih);
      body += `<rect x="${(padL + step * i + (step - bw) / 2).toFixed(1)}" y="${(padT + ih - h).toFixed(1)}"
        width="${bw.toFixed(1)}" height="${h.toFixed(1)}" rx="1"
        fill="${opts.colorOf(r)}"><title>${esc(title)}</title></rect>`;
    }
  });

  // 组合形态：叠一条滑动平均线。462 根柱太密，单看柱子只能看出"高/矮"，
  // 均线才能看出趋势的走向（什么时候开始抬升、什么时候回落）。
  if (opts.avgWindow > 1 && n > opts.avgWindow) {
    const half = Math.floor(opts.avgWindow / 2);
    const avg = vals.map((_, i) => {
      const a = Math.max(0, i - half), b = Math.min(n, i + n % 2 + half);
      const seg = vals.slice(a, b);
      return seg.reduce((x, y) => x + y, 0) / (seg.length || 1);
    });
    body += `<polyline points="${avg.map((v, i) => `${cx(i).toFixed(1)},${cy(v).toFixed(1)}`).join(' ')}"
      fill="none" stroke="${opts.avgColor || '#0f172a'}" stroke-width="2.2" stroke-linejoin="round"/>`;
    avg.forEach((v, i) => {
      if (i % 20 && i !== n - 1) return;
      body += `<circle cx="${cx(i).toFixed(1)}" cy="${cy(v).toFixed(1)}" r="2.6" fill="#fff"
        stroke="${opts.avgColor || '#0f172a'}" stroke-width="1.6"
        ><title>第 ${i + 1} 轮　${opts.avgWindow} 轮滑动平均 ${esc(m.fmt(v))}</title></circle>`;
    });
  }

  const every = Math.max(1, Math.ceil(n / 9));
  let labels = '';
  rows.forEach((r, i) => {
    if (i % every && i !== n - 1) return;
    labels += `<text class="axis-label" x="${cx(i).toFixed(1)}" y="${H - 7}" text-anchor="middle">${esc(fmtTime(r.start))}</text>`;
  });
  el.innerHTML = `<svg width="100%" height="${H}" viewBox="0 0 ${W} ${H}"
    preserveAspectRatio="none">${g}${body}${labels}</svg>`;
}

async function renderTurnChart() {
  const m = M();
  const data = await api('/api/turns', {
    ...filterParams(), limit: 1000, offset: 0, sort: 'start', order: 'asc',
  });
  const rows = data.rows || [];
  const totals = {}, names = {};
  rows.forEach((r) => {
    const k = turnDimKey(r);
    totals[k] = (totals[k] || 0) + (Number(r[m.key]) || 0);
    if (!(k in names)) names[k] = turnDimName(r);
  });
  const topKeys = Object.keys(totals).sort((a, b) => totals[b] - totals[a]).slice(0, 5);
  const colors = {};
  topKeys.forEach((k, i) => { colors[k] = SERIES_COLORS[i % SERIES_COLORS.length]; });

  $('#turnChartHint').textContent = `${fmtNum(rows.length)} 轮（当前筛选）· ${m.label} · `
    + `按${DIM_LABEL[state.dim]}着色　折线＝7 轮滑动平均`
    + (rows.length >= 1000 ? '（仅前 1000 轮）' : '');

  // 固定为「柱+线」：柱=每一轮，线=7 轮滑动平均
  drawTurnChart($('#turnChart'), rows, {
    form: 'bar',
    height: 200,
    colorOf: (r) => colors[turnDimKey(r)] || '#cbd5e1',
    avgWindow: 7,
    avgColor: m.color,
  });

  const grand = rows.reduce((a, r) => a + (Number(r[m.key]) || 0), 0) || 1;
  // 展示名撞车时补短标识（同 分布趋势 的处理：基名截断 + 后缀单独小字）
  const dup = {};
  topKeys.forEach((k) => { const n = names[k] || k; dup[n] = (dup[n] || 0) + 1; });
  const html = topKeys.map((k) => {
    const n = names[k] || k;
    const dis = dup[n] > 1 ? `(${String(k).slice(0, 14)})` : '';
    return `<span class="lg" title="${esc(n + (dis ? ' ' + dis : ''))}"><i style="background:${colors[k]}"></i>`
      + `${esc(oneLine(n, dis ? 12 : 18))}${dis ? `<span class="sub2">${esc(dis)}</span>` : ''}`
      + `<b>${esc(m.fmt(totals[k]))}</b>`
      + `<span class="sub2">${(totals[k] / grand * 100).toFixed(0)}%</span></span>`;
  }).join('');
  const rest = Object.keys(totals).length - topKeys.length;
  $('#turnLegend').innerHTML = html + (rest > 0
    ? `<span class="lg"><i style="background:#cbd5e1"></i>其他 ${rest} 个</span>` : '');
}

/* ---------------------------------------------------------------- 轮次 */
const KIND_TAG = {
  user: ['user', '提问'],
  notify: ['notify', '后台通知'],
  command: ['command', '命令'],
  compact: ['compact', '自动压缩'],
  continue: ['continue', '自动续跑'],
  orphan: ['orphan', '后台任务'],
};

function renderTurns(data, s) {
  const tb = $('#turnsTable tbody');
  state.turnsTotal = data.total;
  const m = M();
  $('#turnsHint').textContent =
    `${fmtNum(data.total)} 轮（当前筛选）· 按${m.label}排序`;
  if (!data.rows.length) {
    tb.innerHTML = '<tr><td colspan="13" class="dim" style="padding:26px 0;text-align:center">当前筛选下没有轮次</td></tr>';
    renderPager(); return;
  }
  const hotTok = metric === 'tokens' ? ' hot tokens' : '';
  const hotCre = metric === 'credit' ? ' hot credit' : '';
  tb.innerHTML = data.rows.map((r) => {
    const kt = KIND_TAG[r.kind] || KIND_TAG.user;
    const models = (r.modelNames || []).slice(0, 2).map((x) => `<span class="tag model">${esc(x)}</span>`).join(' ') +
      ((r.modelNames || []).length > 2 ? ` <span class="tag">+${r.modelNames.length - 2}</span>` : '');
    return `<tr data-id="${esc(r.id)}" class="row">
      <td class="mono" title="${fmtFull(r.start)}">${fmtTime(r.start)}</td>
      <td class="mono">#${r.index || '—'}</td>
      <td><span class="tag ${kt[0]}">${kt[1]}</span></td>
      <td><span class="ellip on" title="点击展开该轮每次请求">${esc(oneLine(r.prompt, 58))}</span>
        <span class="tag" style="margin-top:4px">${models}</span></td>
      <td><span class="ellip" title="${esc(r.sessionTitle)}">${esc(oneLine(r.sessionTitle || r.session, 22))}</span>
        <span class="sub2 dim">${esc(r.project)}</span></td>
      <td class="num mono">${fmtNum(r.req)}</td>
      <td class="num mono">${r.tools ? fmtNum(r.tools) : '—'}</td>
      <td class="num mono">${fmtNum(r.inp)}</td>
      <td class="num mono">${fmtNum(r.out)}</td>
      <td class="num mono">${fmtNum(r.cached)}</td>
      <td class="num mono${hotTok}"><b>${fmtNum(r.total)}</b></td>
      <td class="num mono${hotCre}"><b>${fmtCredit(r.credit)}</b></td>
      <td class="num mono">${fmtDur(r.duration)}</td>
    </tr>`;
  }).join('');

  tb.querySelectorAll('tr.row').forEach((tr) => {
    tr.addEventListener('click', () => toggleTurn(tr, tr.dataset.id));
  });
  markScrollable('#turnsTable', '#turnsScroll');
  renderPager();
}

/* 13 列的表格在窄屏/大字号下必然会横向溢出 —— 直接告诉用户"可以左右滑"，
   否则容易被当成内容缺失 */
function markScrollable(tableSel, hintSel) {
  const tb = $(tableSel);
  const h = $(hintSel);
  const w = tb && tb.closest('.tablewrap');
  if (!w || !h) return;
  h.textContent = (w.scrollWidth - w.clientWidth) > 8 ? '← 可左右滚动 →' : '';
}

function renderPager() {
  const pages = Math.max(1, Math.ceil(state.turnsTotal / state.limit));
  $('#pageInfo').textContent =
    `第 ${state.page + 1} / ${pages} 页 · 共 ${fmtNum(state.turnsTotal)} 轮 · 每页 ${state.limit}`;
  $('#prevPage').disabled = state.page <= 0;
  $('#nextPage').disabled = state.page >= pages - 1;
}

async function toggleTurn(tr, id) {
  const next = tr.nextElementSibling;
  if (next && next.classList.contains('subrow')) {
    next.remove(); tr.classList.remove('expanded'); return;
  }
  tr.classList.add('expanded');
  const sub = document.createElement('tr');
  sub.className = 'subrow';
  sub.innerHTML = '<td colspan="13"><div class="subwrap">加载该轮明细…</div></td>';
  tr.after(sub);
  try {
    const d = await api('/api/turn', { ...filterParams(), id });
    const reqs = d.requests || [];
    const scenes = {};
    ((state.meta && state.meta.scenes) || []).forEach((x) => { scenes[x.id] = x.label; });
    const rows = reqs.map((r) => `<tr>
      <td class="mono">${r.seq}</td>
      <td class="mono">${fmtTime(r.ts, true)}</td>
      <td title="${esc(r.model)}">${esc(r.modelName)}</td>
      <td><span class="tag model" title="档位标识 ${esc(r.scene)}">${esc(scenes[r.scene] || r.scene)}</span></td>
      <td class="num">${fmtNum(r.inp)}</td>
      <td class="num">${fmtNum(r.out)}</td>
      <td class="num">${fmtNum(r.cached)}</td>
      <td class="num">${fmtNum(r.reasoning)}</td>
      <td class="num"><b>${fmtNum(r.total)}</b></td>
      <td class="num">${r.credit.toFixed(2)}</td>
      <td class="mono" title="messageId ${esc(r.msg)}
traceId ${esc(r.trace)}
conversationRequestId ${esc(r.creq)}">${esc((r.msg || '').slice(0, 12))}</td>
    </tr>`).join('');
    sub.querySelector('.subwrap').innerHTML = `
      <h4>该轮共 ${reqs.length} 次 API 请求</h4>
      <div class="prompt">${esc(d.turn.prompt || '—')}</div>
      <table class="subtable">
        <thead><tr><th>#</th><th>时间</th><th>模型</th><th>档位</th>
          <th class="num">输入</th><th class="num">输出</th><th class="num">缓存命中</th>
          <th class="num">思考</th><th class="num">合计</th><th class="num">积分</th>
          <th>请求ID</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>`;
  } catch (e) {
    sub.querySelector('.subwrap').textContent = '加载失败：' + e.message;
  }
}

/* ---------------------------------------------------------------- 会话 */
function renderSessions(s) {
  const m = M();
  const rows = [...(s.by_session || [])].sort((a, b) => b[m.key] - a[m.key]);
  const hotTok = metric === 'tokens' ? ' hot tokens' : '';
  const hotCre = metric === 'credit' ? ' hot credit' : '';
  $('#sessionsHint').textContent = `${rows.length} 个会话 · 按${m.label}排序`;
  $('#sessionsTable tbody').innerHTML = rows.map((r) => `<tr>
    <td><span class="ellip" title="${esc(r.title || r.key)}">${esc(r.title || '(无标题)')}</span>
      <span class="sub2 dim mono">${esc(r.key.slice(0, 8))}</span></td>
    <td class="dim">${esc(r.cwd)}</td>
    <td class="mono">${fmtTime(r.start)}</td>
    <td class="mono">${fmtTime(r.end)}</td>
    <td class="num mono">${fmtNum(r.turns)}</td>
    <td class="num mono">${fmtNum(r.req)}</td>
    <td class="num mono">${fmtNum(r.inp)}</td>
    <td class="num mono">${fmtNum(r.out)}</td>
    <td class="num mono${hotTok}"><b>${fmtNum(r.total)}</b></td>
    <td class="num mono${hotCre}"><b>${fmtCredit(r.credit)}</b></td>
    <td><button class="ghost" data-sess="${esc(r.key)}">看轮次</button></td>
  </tr>`).join('') || '<tr><td colspan="11" class="dim">无数据</td></tr>';
  markScrollable('#sessionsTable', '#sessionsScroll');

  $('#sessionsTable tbody').querySelectorAll('button[data-sess]').forEach((b) => {
    b.addEventListener('click', () => {
      $('#fSession').value = b.dataset.sess;
      state.page = 0;
      switchTab('turns');
      refresh();
    });
  });
}

/* ---------------------------------------------------------------- 模型 */
function renderModels(s) {
  const m = M();
  const hotTok = metric === 'tokens' ? ' hot tokens' : '';
  const hotCre = metric === 'credit' ? ' hot credit' : '';
  const wrap = $('#sceneTable');
  const scenes = [...(s.scene_models || [])].sort((a, b) => b[m.key] - a[m.key]);

  wrap.innerHTML = scenes.map((sc) => {
    const models = [...sc.models].sort((a, b) => b[m.key] - a[m.key]);
    const sceneTotal = sc[m.key] || 1;
    const sub = models.map((x) => `<tr>
        <td>${esc(x.name)}<span class="sub2 dim"> ${esc(x.id)}</span></td>
        <td class="num">${fmtNum(x.req)}</td>
        <td class="num${hotTok}">${fmtTok(x.total)}</td>
        <td class="num${hotCre}">${x.credit.toFixed(2)}</td>
        <td class="num">${((x[m.key] || 0) / sceneTotal * 100).toFixed(1)}%</td>
      </tr>`).join('');
    const nums = metric === 'credit'
      ? `<span><b>${fmtCredit(sc.credit)} 积分</b></span><span>${fmtTok(sc.total)} tokens</span>`
      : `<span><b>${fmtTok(sc.total)} tokens</b></span><span>${fmtCredit(sc.credit)} 积分</span>`;
    return `<div class="scene">
      <div class="head">
        <b>${esc(sc.label)}</b>
        <span class="tag model">${esc(sc.scene)}</span>
        ${sc.auto ? '<span class="tag user">自动档位</span>' : '<span class="tag">指定模型</span>'}
        <span class="spacer">
          <span>${fmtNum(sc.req)} 请求</span>
          ${nums}
        </span>
      </div>
      <table class="subtable">
        <thead><tr><th>实际承载模型</th><th class="num">请求</th>
          <th class="num${hotTok}">合计 Tokens</th>
          <th class="num${hotCre}">积分</th>
          <th class="num">占比（按${esc(m.label)}）</th></tr></thead>
        <tbody>${sub}</tbody>
      </table>
    </div>`;
  }).join('') || '<p class="note">无数据</p>';

  const modelRows = [...(s.by_model || [])].sort((a, b) => b[m.key] - a[m.key]);
  $('#modelTable tbody').innerHTML = modelRows.map((r) => `<tr>
    <td>${esc(r.name)}<span class="sub2 dim mono"> ${esc(r.key)}</span></td>
    <td class="num mono">${fmtNum(r.req)}</td>
    <td class="num mono">${fmtNum(r.inp)}</td>
    <td class="num mono">${fmtNum(r.out)}</td>
    <td class="num mono">${fmtNum(r.cached)}</td>
    <td class="num mono">${fmtNum(r.reasoning)}</td>
    <td class="num mono${hotTok}"><b>${fmtNum(r.total)}</b></td>
    <td class="num mono${hotCre}"><b>${r.credit.toFixed(2)}</b></td>
    <td class="num mono">${(r.credit / (r.req || 1)).toFixed(3)}</td>
  </tr>`).join('') || '<tr><td colspan="9" class="dim">无数据</td></tr>';
  markScrollable('#modelTable', '#modelScroll');
}

/* ---------------------------------------------------------------- 刷新 */
let refreshing = false;
async function refresh() {
  if (refreshing) return;
  refreshing = true;
  try {
    const s = await api('/api/summary');
    state.summary = s;
    renderKpis(s);
    renderMonth(s);
    $('#rangeText').textContent =
      (s.range[0] ? `${s.range[0]} ~ ${s.range[1]}` : '无数据') +
      ` · 更新于 ${s.generated_at.slice(11)}`;
    if (state.tab === 'overview') await renderOverview(s);
    if (state.tab === 'sessions') renderSessions(s);
    if (state.tab === 'models') renderModels(s);
    if (state.tab === 'turns') {
      const t = await api('/api/turns', {
        ...filterParams(), limit: state.limit, offset: state.page * state.limit,
        sort: state.sort.k, order: state.sort.order,
      });
      renderTurns(t, s);
      await renderTurnChart();      // 图表要看全部轮次，单独拉一份
    }
    syncMetricColumns();
  } catch (e) {
    toast('刷新失败：' + e.message);
  } finally {
    refreshing = false;
  }
}

/* ---------------------------------------------------------------- 实时 */
let pending = 0, debounceTimer = null;
function connectStream() {
  // ?nolive=1 关闭实时推送（也给无头截图留一条可静止的路径）
  if (new URLSearchParams(location.search).get('nolive')) {
    setLive(false, '实时已关闭（?nolive=1）');
    return;
  }
  const es = new EventSource('/api/stream');
  es.onopen = () => { state.liveOk = true; setLive(true, '实时已连接'); };
  es.onerror = () => { state.liveOk = false; setLive(false, '连接中断，正在重连…'); };
  es.onmessage = (ev) => {
    let d;
    try { d = JSON.parse(ev.data); } catch { return; }
    if (d.type !== 'update') return;
    const grew = d.requests - (state.lastSeen || d.requests);
    const first = state.lastSeen == null;
    state.lastSeen = d.requests;
    state.lastTs = d.lastTs;
    setLive(true, `实时 · 最后扫描 ${fmtTime(d.scanAt, true)}`);
    if (!first && grew > 0) {
      pending = grew;
      toast(`检测到新增 ${grew} 条请求，正在刷新…`);
      clearTimeout(debounceTimer);
      debounceTimer = setTimeout(() => { pending = 0; refresh(); }, 900);
    }
  };
}

function setLive(ok, text) {
  $('#liveDot').className = 'dot ' + (ok ? 'live' : 'err');
  $('#liveText').textContent = text;
}

let toastTimer = null;
function toast(msg) {
  const el = $('#toast');
  el.textContent = msg;
  el.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('show'), 2600);
}

/* ---------------------------------------------------------------- 交互 */
const TABS = ['overview', 'turns', 'sessions', 'models'];

/* 表头里代表当前口径的那一列加粗上色 */
function syncMetricColumns() {
  const m = M();
  document.querySelectorAll('[data-metric-col]').forEach((el) => {
    const key = el.dataset.metricCol;
    el.classList.toggle('hot', key === m.key);
    el.classList.toggle('credit', key === 'credit');
    el.classList.toggle('tokens', key === 'total');
  });
}

/* 表头的排序箭头跟着 state.sort 走（切换口径后默认按新口径排序） */
function syncSortIndicator() {
  document.querySelectorAll('#turnsTable th.sortable').forEach((th) => {
    const base = th.textContent.replace(/ [▲▼]$/, '');
    const on = th.dataset.k === state.sort.k;
    th.classList.toggle('on', on);
    th.textContent = base + (on ? (state.sort.order === 'desc' ? ' ▼' : ' ▲') : '');
  });
}

/* 把当前视图（口径 / 维度 / 日期 / 图表形态）同步进 URL，便于分享与刷新保持 */
function syncUrl() {
  try {
    const u = new URL(location.href);
    u.searchParams.set('metric', metric);
    if (state.dim !== 'model') u.searchParams.set('dim', state.dim);
    else u.searchParams.delete('dim');
    if (state.hourDay) u.searchParams.set('day', state.hourDay);
    else u.searchParams.delete('day');
    if (state.range !== 'all') u.searchParams.set('range', state.range);
    else u.searchParams.delete('range');
    if (state.rangeKey && state.range !== 'all') u.searchParams.set('rkey', state.rangeKey);
    else u.searchParams.delete('rkey');
    u.searchParams.delete('dayform');
    u.searchParams.delete('dimform');
    history.replaceState(null, '', u.toString());
  } catch (e) { /* 忽略 */ }
}

function setMetric(next) {
  if (next !== 'credit' && next !== 'tokens') return;
  metric = next;
  try { localStorage.setItem(METRIC_STORE, metric); } catch (e) { /* 忽略 */ }
  document.querySelectorAll('#metricSwitch button').forEach((b) =>
    b.classList.toggle('on', b.dataset.metric === metric));
  // 口径变了，默认排序也跟着变：想看的永远是"最费的是哪几个"
  state.sort = { k: M().key, order: 'desc' };
  state.page = 0;
  syncSortIndicator();
  syncMetricColumns();
  syncUrl();
  refresh();
}

function setDim(next) {
  if (!DIM_LABEL[next] || next === state.dim) return;
  state.dim = next;
  syncSwitchUI();
  syncUrl();
  refresh();
}

/* 维度开关是全页共用的（总览的每日消耗分布 与 轮次分布图 共用同一个维度） */
function syncSwitchUI() {
  document.querySelectorAll('[data-dim]').forEach((b) =>
    b.classList.toggle('on', b.dataset.dim === state.dim));
  document.querySelectorAll('[data-range]').forEach((b) =>
    b.classList.toggle('on', b.dataset.range === state.range));
}

function switchTab(tab) {
  if (TABS.indexOf(tab) < 0) tab = 'overview';
  state.tab = tab;
  document.querySelectorAll('#tabs button').forEach((b) =>
    b.classList.toggle('on', b.dataset.tab === tab));
  document.querySelectorAll('.panel').forEach((p) =>
    p.classList.toggle('hide', p.id !== 'panel-' + tab));
  if (location.hash.slice(1) !== tab) history.replaceState(null, '', '#' + tab);
  refresh();
}

function fillSelect(sel, items, allLabel) {
  const cur = sel.value;
  sel.innerHTML = `<option value="">${allLabel}</option>` +
    items.map((o) => `<option value="${esc(o.v)}">${esc(o.t)}</option>`).join('');
  if ([...sel.options].some((o) => o.value === cur)) sel.value = cur;
}

function bind() {
  document.querySelectorAll('#tabs button').forEach((b) =>
    b.addEventListener('click', () => switchTab(b.dataset.tab)));

  ['#fFrom', '#fTo', '#fProject', '#fSession', '#fModel', '#fScene'].forEach((s) =>
    $(s).addEventListener('change', () => { state.page = 0; refresh(); }));
  let qt = null;
  $('#fQ').addEventListener('input', () => {
    clearTimeout(qt); qt = setTimeout(() => { state.page = 0; refresh(); }, 320);
  });
  $('#btnReset').addEventListener('click', () => {
    ['#fFrom', '#fTo', '#fProject', '#fSession', '#fModel', '#fScene', '#fQ']
      .forEach((s) => { $(s).value = ''; });
    state.page = 0; refresh();
  });
  $('#btnRefresh').addEventListener('click', refresh);
  $('#btnCsv').addEventListener('click', () => {
    const u = new URL('/api/export.csv', location.origin);
    for (const [k, v] of Object.entries(filterParams())) if (v) u.searchParams.set(k, v);
    location.href = u.toString();
  });
  $('#prevPage').addEventListener('click', () => { if (state.page > 0) { state.page--; refresh(); } });
  $('#nextPage').addEventListener('click', () => { state.page++; refresh(); });

  document.querySelectorAll('#turnsTable th.sortable').forEach((th) => {
    th.addEventListener('click', () => {
      const k = th.dataset.k;
      if (state.sort.k === k) {
        state.sort.order = state.sort.order === 'desc' ? 'asc' : 'desc';
      } else {
        state.sort = { k, order: 'desc' };
      }
      syncSortIndicator();
      state.page = 0;
      refresh();
    });
  });

  document.querySelectorAll('#metricSwitch button').forEach((b) => {
    b.addEventListener('click', () => setMetric(b.dataset.metric));
  });

  // 分布趋势的维度切换（模型 / 项目 / 会话）
  document.querySelectorAll('#dimSwitch button').forEach((b) => {
    b.addEventListener('click', () => setDim(b.dataset.dim));
  });

  syncSwitchUI();

  // 分布图粒度：全部 / 按月 / 按周 / 按日
  $('#rangeSwitch').addEventListener('click', (e) => {
    const b = e.target.closest('button[data-range]');
    if (!b || b.dataset.range === state.range) return;
    state.range = b.dataset.range;
    state.rangeKey = '';        // 换粒度后重新回落到最新一个周期
    syncSwitchUI();
    syncUrl();
    refresh();
  });
  // 共用一个下拉：选项随粒度变
  $('#rangePick').addEventListener('change', () => {
    state.rangeKey = $('#rangePick').value;
    syncUrl();
    refresh();
  });

  // 24 小时分布切换日期
  $('#hourDay').addEventListener('change', () => {
    state.hourDay = $('#hourDay').value;
    syncUrl();
    refresh();
  });

  let rt = null;
  window.addEventListener('resize', () => {
    clearTimeout(rt);
    rt = setTimeout(() => {
      if (state.tab === 'overview') { refresh(); return; }
      // 表格页：窗口变窄/变宽后重新判断要不要提示横向滚动
      markScrollable('#turnsTable', '#turnsScroll');
      markScrollable('#sessionsTable', '#sessionsScroll');
      markScrollable('#modelTable', '#modelScroll');
    }, 220);
  });

  window.addEventListener('hashchange', () => {
    const t = location.hash.slice(1);
    if (TABS.indexOf(t) >= 0 && t !== state.tab) switchTab(t);
  });
}

async function init() {
  bind();
  // 视图状态优先级：URL 参数 > localStorage > 默认值
  try {
    const qs = new URLSearchParams(location.search);
    const qm = qs.get('metric');
    if (qm === 'credit' || qm === 'tokens') metric = qm;
    const qd = qs.get('dim');
    if (DIM_LABEL[qd]) state.dim = qd;
    const qh = qs.get('day');
    if (qh) state.hourDay = qh;
    const qr = qs.get('range');
    if (['all', 'month', 'week', 'day'].indexOf(qr) >= 0) state.range = qr;
    const qk = qs.get('rkey');
    if (qk) state.rangeKey = qk;
  } catch (e) { /* 忽略 */ }
  document.querySelectorAll('#metricSwitch button').forEach((b) =>
    b.classList.toggle('on', b.dataset.metric === metric));
  syncSwitchUI();
  state.sort = { k: M().key, order: 'desc' };
  syncSortIndicator();
  syncMetricColumns();

  const want = location.hash.slice(1);
  if (TABS.indexOf(want) >= 0) {
    state.tab = want;
    document.querySelectorAll('#tabs button').forEach((b) =>
      b.classList.toggle('on', b.dataset.tab === want));
    document.querySelectorAll('.panel').forEach((p) =>
      p.classList.toggle('hide', p.id !== 'panel-' + want));
  }
  const m = await api('/api/meta', {});
  state.meta = m;
  renderIdents();
  fillSelect($('#fProject'), m.projects.map((p) => ({ v: p.cwd, t: `${p.name}（${p.req}）` })), '全部项目');
  fillSelect($('#fSession'), m.sessions.map((s) => ({ v: s.id, t: (s.title || s.id.slice(0, 8)) + `（${s.req}）` })), '全部会话');
  fillSelect($('#fModel'), m.models.map((x) => ({ v: x.id, t: `${x.name}（${x.req}）` })), '全部模型');
  fillSelect($('#fScene'), m.scenes.map((x) => ({ v: x.id, t: `${x.label}（${x.req}）` })), '全部档位');
  if (m.range[0]) { $('#fFrom').value = ''; $('#fTo').value = ''; }
  $('#footInfo').textContent =
    `${fmtNum(m.counts.requests)} 条 LLM 请求 · ${fmtNum(m.counts.prompts)} 条提问 · ` +
    `${fmtNum(m.counts.tools)} 次工具调用 · ${m.counts.days} 天 · 数据版本 ${m.dataVersion}`;
  await refresh();
  connectStream();
}

init().catch((e) => {
  document.body.insertAdjacentHTML('afterbegin',
    `<p style="padding:16px;color:#b91c1c">初始化失败：${esc(e.message)}</p>`);
});
