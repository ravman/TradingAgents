/* TradingAgents Control Center — front end */
'use strict';

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const fmtN = (n, d = 0) => n == null || isNaN(n) ? '—' : Number(n).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d });
const fmtQ = n => n == null ? '—' : Number(n).toLocaleString(undefined, { maximumFractionDigits: Number.isInteger(+n) ? 0 : 4 });
const fmtK = n => n == null ? '—' : n >= 1e6 ? (n / 1e6).toFixed(2) + 'M' : n >= 1e3 ? (n / 1e3).toFixed(1) + 'k' : String(n);
const fmtDur = s => s == null ? '—' : s < 60 ? s.toFixed(0) + 's' : Math.floor(s / 60) + 'm ' + String(Math.floor(s % 60)).padStart(2, '0') + 's';
const fmtTime = ts => new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
const today = () => { const d = new Date(); return new Date(d - d.getTimezoneOffset() * 6e4).toISOString().slice(0, 10); };
const cssv = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const md = t => DOMPurify.sanitize(marked.parse(String(t || '')));

async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { 'content-type': 'application/json' }, ...opts,
    body: opts.body && typeof opts.body !== 'string' ? JSON.stringify(opts.body) : opts.body });
  if (!r.ok) { let m = r.statusText; try { m = (await r.json()).detail || m; } catch (e) {} throw new Error(m); }
  return r.json();
}
function toast(msg, ms = 4000) {
  const d = document.createElement('div'); d.innerHTML = msg; $('#toast').appendChild(d); setTimeout(() => d.remove(), ms);
}

// ------------------------------------------------------------------ constants
const TEAMS = [
  { name: 'Analyst team', color: '--team-analyst', agents: ['Market Analyst', 'Sentiment Analyst', 'News Analyst', 'Fundamentals Analyst'] },
  { name: 'Research team', color: '--team-research', agents: ['Bull Researcher', 'Bear Researcher', 'Research Manager'] },
  { name: 'Trading', color: '--team-trader', agents: ['Trader'] },
  { name: 'Risk & portfolio', color: '--team-risk', agents: ['Aggressive Analyst', 'Conservative Analyst', 'Neutral Analyst', 'Portfolio Manager'] },
];
const TEAM_OF = {}; TEAMS.forEach(t => t.agents.forEach(a => TEAM_OF[a] = t));
const ALL_AGENTS = TEAMS.flatMap(t => t.agents);
const REPORTS = [
  ['market_report', 'Market'], ['sentiment_report', 'Sentiment'], ['news_report', 'News'], ['fundamentals_report', 'Fundamentals'],
  ['_debate_research', 'Bull vs Bear'], ['investment_plan', 'Research plan'], ['trader_investment_plan', 'Trader'],
  ['_debate_risk', 'Risk debate'], ['final_trade_decision', 'Final decision'],
];
const BULLISH = ['Buy', 'Overweight'], BEARISH = ['Sell', 'Underweight'];
const ratingBadge = r => r ? `<span class="badge ${esc(r.toLowerCase())}">${BULLISH.includes(r) ? '▲' : BEARISH.includes(r) ? '▼' : '●'} ${esc(r)}</span>` : '';
const statusBadge = s => `<span class="badge st-${esc(s)}">${esc(s)}</span>`;

// ------------------------------------------------------------------ state
const S = {
  meta: null, settings: null, runs: {}, current: null, rs: {}, focus: null, follow: true,
  ticker: 'RELIANCE.NS', interval: '1d', overlays: new Set(['sma', 'marks']), indPane: 'rsi',
  quotes: {}, portfolio: null, feedFilter: 'all', reportTab: 'market_report', batches: {}, bars: [],
};

// ------------------------------------------------------------------ run state (event reducer)
function newRunState(summary) {
  return { id: summary.id, summary, t0: summary.started || null, tEnd: null, pipeline: null, active: null,
    agents: {}, feed: [], reports: {}, debate: [], stats: summary.stats || {}, tokens: [], dataChars: 0,
    decision: null, loaded: false };
}
function agentOf(rs, a) {
  return rs.agents[a] || (rs.agents[a] = { status: 'pending', spans: [], llm: [], tools: [], busy: 0 });
}
function closeActive(rs, ts, status = 'done') {
  if (!rs.active) return;
  const ag = agentOf(rs, rs.active); const sp = ag.spans[ag.spans.length - 1];
  if (sp && sp[1] == null) sp[1] = ts;
  ag.llm.forEach(l => { if (l[1] == null) l[1] = ts; });
  ag.status = status; rs.active = null;
}
function applyEvent(rs, ev) {
  const ts = ev.ts; const a = ev.agent;
  switch (ev.type) {
    case 'run_queued': rs.summary = ev.run; break;
    case 'run_started':
      rs.summary = ev.run; rs.t0 = ts; rs.pipeline = ev.pipeline;
      ALL_AGENTS.forEach(n => agentOf(rs, n).status = ev.pipeline.includes(n) ? 'pending' : 'skipped');
      rs.feed.push({ ts, kind: 'system', who: 'System', text: `Run started · ${ev.run.ticker} · ${ev.run.date}` });
      break;
    case 'node_start':
      if (!a) break;
      if (rs.active !== a) {
        closeActive(rs, ts);
        const ag = agentOf(rs, a); ag.status = 'active'; ag.spans.push([ts, null]); rs.active = a;
        rs.feed.push({ ts, kind: 'agent', who: a, text: 'started' });
      }
      break;
    case 'llm_start': if (a) agentOf(rs, a).llm.push([ts, null, 0]); if (ev.stats) rs.stats = ev.stats; break;
    case 'llm_end':
      if (a) { const l = agentOf(rs, a).llm.find(x => x[1] == null); if (l) { l[1] = ts; l[2] = ev.tokens_in + ev.tokens_out; } }
      if (ev.stats) { rs.stats = ev.stats; rs.tokens.push([ts, ev.stats.tokens_in + ev.stats.tokens_out]); }
      break;
    case 'llm_error': rs.feed.push({ ts, kind: 'error', who: a || 'LLM', text: ev.error }); break;
    case 'tool_start':
      if (a) agentOf(rs, a).tools.push([ts, ev.tool]);
      if (ev.stats) rs.stats = ev.stats;
      rs.feed.push({ ts, kind: 'tool', who: a || 'Tool', tool: ev.tool, text: typeof ev.args === 'object' ? JSON.stringify(ev.args) : ev.args });
      break;
    case 'tool_end':
      rs.dataChars += ev.chars || 0;
      rs.feed.push({ ts, kind: 'data', who: a || 'Tool', tool: ev.tool, text: `${fmtK(ev.chars)} chars in ${ev.duration}s`, detail: ev.output });
      break;
    case 'tool_error': rs.feed.push({ ts, kind: 'error', who: a || 'Tool', tool: ev.tool, text: ev.error }); break;
    case 'message':
      if (ev.kind === 'reasoning') rs.feed.push({ ts, kind: 'reasoning', who: rs.active || 'Agent', text: ev.content });
      break;
    case 'report': rs.reports[ev.key] = ev.content; break;
    case 'debate': rs.debate.push(ev); rs.feed.push({ ts, kind: 'debate', who: ev.speaker, text: ev.content }); break;
    case 'context': rs.feed.push({ ts, kind: 'system', who: 'Instrument', text: 'Resolved instrument identity', detail: ev.text }); break;
    case 'memory': rs.feed.push({ ts, kind: 'system', who: 'Memory', text: 'Past decisions & lessons injected', detail: ev.text }); break;
    case 'log': rs.feed.push({ ts, kind: ev.level === 'warn' ? 'error' : 'system', who: 'System', text: ev.text }); break;
    case 'decision':
      rs.decision = ev; closeActive(rs, ts);
      rs.feed.push({ ts, kind: 'decision', who: 'Portfolio Manager', text: `Final rating: ${ev.signal}` });
      break;
    case 'trade': rs.feed.push({ ts, kind: 'trade', who: 'Paper ledger', text: tradeText(ev.trade) }); break;
    case 'run_finished': case 'run_failed': case 'run_cancelled':
      rs.summary = ev.run; rs.tEnd = ts;
      closeActive(rs, ts, ev.type === 'run_failed' ? 'error' : 'done');
      if (ev.type !== 'run_finished') rs.feed.push({ ts, kind: 'error', who: 'System', text: ev.type === 'run_failed' ? `Run failed: ${ev.run.error}` : 'Run cancelled', detail: ev.trace });
      else rs.feed.push({ ts, kind: 'system', who: 'System', text: `Run finished in ${fmtDur(ts - rs.t0)}` });
      break;
  }
}
const tradeText = t => !t ? '' : t.side === 'NONE' ? `${t.ticker}: ${t.rating} → no change (${t.note})` : `${t.side} ${fmtQ(t.qty)} ${t.ticker} @ ${fmtN(t.price, 2)} (${t.rating})`;

// ------------------------------------------------------------------ websocket
let ws, wsRetry = 0;
function connect() {
  ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws');
  ws.onopen = () => { wsRetry = 0; setConn(true); subscribe(); };
  ws.onclose = () => { setConn(false); setTimeout(connect, Math.min(10000, 1000 * ++wsRetry)); };
  ws.onmessage = m => onEvent(JSON.parse(m.data));
}
function subscribe() { if (ws && ws.readyState === 1) ws.send(JSON.stringify({ type: 'subscribe', tickers: [S.ticker] })); }
function setConn(on) { $('#connDot').className = 'dot ' + (on ? 'on' : 'off'); $('#connText').textContent = on ? 'Live' : 'Reconnecting…'; }

let renderPending = false;
function scheduleRender() { if (renderPending) return; renderPending = true; requestAnimationFrame(() => { renderPending = false; renderFocus(); }); }

function onEvent(ev) {
  switch (ev.type) {
    case 'quote': S.quotes[ev.ticker] = ev; renderWatchQuote(ev.ticker); if (ev.ticker === S.ticker) liveQuote(ev); return;
    case 'bar': if (ev.ticker === S.ticker && S.interval === '1m' && candle) { try { candle.update(ev.bar); } catch (e) {} } return;
    case 'portfolio': S.portfolio = ev.portfolio; renderPortfolio(); return;
    case 'batch': S.batches[ev.batch.id] = ev.batch; renderBacktests(); return;
    case 'settings': S.settings = ev.settings; renderProviderPill(); renderWatchlist(); return;
  }
  if (ev.run) { S.runs[ev.run.id] = ev.run; }
  const rid = ev.run_id;
  if (rid) {
    if (!S.rs[rid] && S.runs[rid]) { S.rs[rid] = newRunState(S.runs[rid]); S.rs[rid].loaded = true; }
    const rs = S.rs[rid];
    if (rs) {
      const before = rs.feed.length;
      applyEvent(rs, ev);
      if (S.focus === rid) { appendFeed(rs, before); scheduleRender(); }
    }
  }
  if (ev.type === 'run_started') {
    S.current = rid;
    if (S.follow || !S.focus) focusRun(rid, false);
  }
  if (['run_finished', 'run_failed', 'run_cancelled'].includes(ev.type)) {
    if (S.current === rid) S.current = null;
    if (ev.type === 'run_finished') {
      toast(`<b>${esc(ev.run.ticker)}</b> ${esc(ev.run.date)} → ${ratingBadge(ev.run.signal)}`);
      if (ev.run.ticker === S.ticker) setMarkers();
    } else if (ev.type === 'run_failed') toast(`<b>${esc(ev.run.ticker)}</b> failed: ${esc(ev.run.error)}`, 8000);
  }
  if (ev.type === 'trade') { S.portfolio = ev.portfolio; renderPortfolio(); if (ev.trade && ev.trade.ticker === S.ticker) setMarkers(); }
  if (ev.run || ev.type === 'run_started') renderQueue();
}

// ------------------------------------------------------------------ focus / render run
async function focusRun(rid, switchTab = true) {
  S.focus = rid;
  const sum = S.runs[rid];
  if (!S.rs[rid] || !S.rs[rid].loaded) {
    const full = await api('/api/runs/' + rid);
    const rs = newRunState(full); rs.loaded = true;
    full.events.forEach(e => applyEvent(rs, e));
    S.rs[rid] = rs;
  }
  if (switchTab) showView('live');
  if (sum && sum.ticker !== S.ticker) setTicker(sum.ticker);
  $('#feed').innerHTML = ''; appendFeed(S.rs[rid], 0);
  renderFocus(); renderQueue();
}

function renderFocus() {
  const rs = S.rs[S.focus];
  const sim = rs ? rs.summary.simulated : S.settings?.llm_provider === 'simulation';
  $('#simBanner').style.display = sim ? '' : 'none';
  if (!rs) return;
  const s = rs.summary;
  $('#runHead').innerHTML = `<b>${esc(s.ticker)}</b> · ${esc(s.date)} ${statusBadge(s.status)} ${s.simulated ? '<span class="badge sim">SIM</span>' : ''}`;
  $('#cancelRunBtn').style.display = ['queued', 'running'].includes(s.status) ? '' : 'none';
  renderDecision(rs); renderTiles(rs); renderPipeline(rs); renderTimeline(rs); renderReports(rs); renderTokenChart(rs);
}

function renderDecision(rs) {
  const s = rs.summary; const box = $('#decisionBox');
  if (rs.decision || s.signal) {
    const sig = rs.decision?.signal || s.signal;
    const col = BULLISH.includes(sig) ? 'var(--good-text)' : BEARISH.includes(sig) ? 'var(--critical-text)' : 'var(--text-secondary)';
    const arrow = BULLISH.includes(sig) ? '▲' : BEARISH.includes(sig) ? '▼' : '●';
    const tr = s.trade ? `<div class="sec" style="margin-top:4px">Paper: ${esc(tradeText(s.trade))}</div>` : '';
    box.innerHTML = `<div class="muted" style="font-size:11px">FINAL RATING · ${esc(s.ticker)} · ${esc(s.date)}</div>
      <div class="big" style="color:${col}"><span class="arrow">${arrow}</span>${esc(sig)}</div>${tr}
      <button class="btn small" style="margin-top:8px" onclick="S.reportTab='final_trade_decision';renderReports(S.rs[S.focus]);document.getElementById('rbody').scrollIntoView({behavior:'smooth'})">Read the decision</button>`;
  } else if (s.status === 'running') {
    box.innerHTML = `<div class="muted" style="font-size:11px">IN PROGRESS</div><div class="big" style="font-size:18px"><span class="dot run"></span>${esc(rs.active || 'Starting…')}</div>`;
  } else if (s.status === 'failed') {
    box.innerHTML = `<div class="big" style="font-size:18px;color:var(--critical-text)">Failed</div><div class="sec">${esc(s.error)}</div>`;
  } else box.innerHTML = `<div class="big" style="font-size:18px">${esc(s.status)}</div>`;
}

function renderTiles(rs) {
  const st = rs.stats || {}; const end = rs.tEnd || (rs.summary.status === 'running' ? Date.now() / 1000 : rs.summary.finished);
  $('#tElapsed').textContent = rs.t0 && end ? fmtDur(end - rs.t0) : '—';
  $('#tLlm').textContent = fmtN(st.llm_calls); $('#tTools').textContent = fmtN(st.tool_calls);
  $('#tIn').textContent = fmtK(st.tokens_in); $('#tOut').textContent = fmtK(st.tokens_out);
  $('#tData').textContent = rs.dataChars ? fmtK(rs.dataChars) + ' ch' : '—';
}

function renderPipeline(rs) {
  const now = Date.now() / 1000;
  $('#pipeline').innerHTML = TEAMS.map(t => `<div class="team"><div class="team-name"><span class="sw" style="background:var(${t.color})"></span>${t.name}</div>` +
    t.agents.map(a => {
      const ag = rs.agents[a] || { status: 'pending', spans: [], llm: [], tools: [] };
      const dur = ag.spans.reduce((s, [x, y]) => s + ((y ?? (rs.summary.status === 'running' ? now : x)) - x), 0);
      const meta = ag.spans.length ? `${fmtDur(dur)} · ${ag.llm.length} llm · ${ag.tools.length} tools` : ag.status === 'skipped' ? 'off' : '';
      return `<div class="agent ${ag.status}"><span class="ic"></span><span>${a}</span><span class="meta">${meta}</span></div>`;
    }).join('') + '</div>').join('');
}

// ---- timeline (SVG swimlanes)
function renderTimeline(rs) {
  const el = $('#timeline');
  if (!rs.t0) { el.innerHTML = '<div class="empty">Queued — waiting for the worker.</div>'; return; }
  const lanes = ALL_AGENTS.filter(a => !rs.pipeline || rs.pipeline.includes(a));
  const W = Math.max(320, el.clientWidth), L = 150, R = 12, laneH = 22, top = 8, H = top + lanes.length * laneH + 24;
  const end = rs.tEnd || (rs.summary.status === 'running' ? Date.now() / 1000 : (rs.summary.finished || rs.t0 + 1));
  const span = Math.max(10, end - rs.t0);
  const x = t => L + (t - rs.t0) / span * (W - L - R);
  let g = '';
  // grid + axis
  const step = [5, 10, 15, 30, 60, 120, 300, 600, 900, 1800].find(s => span / s <= 8) || 3600;
  for (let t = 0; t <= span; t += step) {
    g += `<line x1="${x(rs.t0 + t)}" x2="${x(rs.t0 + t)}" y1="${top}" y2="${H - 20}" stroke="var(--grid)"/>`;
    g += `<text class="axis" x="${x(rs.t0 + t)}" y="${H - 6}" text-anchor="middle">${fmtDur(t)}</text>`;
  }
  lanes.forEach((a, i) => {
    const y = top + i * laneH, ag = rs.agents[a] || { spans: [], llm: [], tools: [] }, col = `var(${TEAM_OF[a].color})`;
    g += `<text class="lane-label" x="${L - 8}" y="${y + 15}" text-anchor="end">${a}</text>`;
    g += `<line x1="${L}" x2="${W - R}" y1="${y + laneH - .5}" y2="${y + laneH - .5}" stroke="var(--grid)"/>`;
    ag.spans.forEach(([s, e]) => {
      const e2 = e ?? end;
      g += `<rect x="${x(s)}" y="${y + 4}" width="${Math.max(2, x(e2) - x(s))}" height="${laneH - 8}" rx="3" fill="${col}" opacity=".28" data-tip="${esc(a)}\n${fmtDur(e2 - s)} active"/>`;
    });
    ag.llm.forEach(([s, e, tok]) => {
      const e2 = e ?? end;
      g += `<rect x="${x(s)}" y="${y + 7}" width="${Math.max(2, x(e2) - x(s) - 1)}" height="${laneH - 14}" rx="2" fill="${col}" data-tip="${esc(a)} · LLM call\n${fmtDur(e2 - s)}${tok ? ' · ' + fmtK(tok) + ' tokens' : ''}"/>`;
    });
    ag.tools.forEach(([t, name]) => {
      g += `<circle cx="${x(t)}" cy="${y + laneH / 2}" r="4" fill="var(--surface-1)" stroke="${col}" stroke-width="2" data-tip="${esc(a)} · tool\n${esc(name)}"/>`;
    });
  });
  if (rs.summary.status === 'running') g += `<line x1="${x(end)}" x2="${x(end)}" y1="${top}" y2="${H - 20}" stroke="var(--accent)" stroke-dasharray="3 3"/>`;
  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" height="${H}" role="img" aria-label="Agent activity timeline">${g}</svg>`;
  $('#tlSub').textContent = `${esc(rs.summary.ticker)} · ${fmtDur(span)}`;
}
document.addEventListener('mousemove', e => {
  const t = e.target.closest && e.target.closest('[data-tip]'); const tip = $('#tip');
  if (!t) { tip.style.display = 'none'; return; }
  tip.textContent = t.getAttribute('data-tip'); tip.style.display = 'block';
  tip.style.left = Math.min(innerWidth - 370, e.clientX + 12) + 'px'; tip.style.top = (e.clientY + 14) + 'px';
});

// ---- feed
function feedMatch(it) {
  const f = S.feedFilter;
  return f === 'all' || (f === 'reasoning' && ['reasoning', 'decision', 'agent'].includes(it.kind)) ||
    (f === 'tools' && ['tool', 'data'].includes(it.kind)) || (f === 'debate' && ['debate', 'decision'].includes(it.kind));
}
function feedItemHTML(it) {
  const col = TEAM_OF[it.who] ? `var(${TEAM_OF[it.who].color})` : 'var(--text-muted)';
  const long = it.text && it.text.length > 280;
  let body;
  if (it.kind === 'reasoning' || it.kind === 'debate') {
    body = long ? `<details><summary>${esc(it.text.slice(0, 220))}…</summary><div class="md" style="padding:6px 0;max-height:none">${md(it.text)}</div></details>` : esc(it.text);
  } else if (it.detail) {
    body = `<details><summary>${it.tool ? `<span class="tag">${esc(it.tool)}</span>` : ''}${esc(it.text)}</summary><pre>${esc(it.detail)}</pre></details>`;
  } else body = (it.tool ? `<span class="tag">${esc(it.tool)}</span>` : '') + esc(it.text);
  const kindTag = { tool: 'call', data: 'data', debate: 'debate', decision: 'decision', trade: 'trade', error: 'error' }[it.kind];
  return `<div class="ev k-${it.kind}"><span class="ts">${fmtTime(it.ts).slice(0, 8)}</span><div><span class="who"><span class="sw" style="background:${col}"></span>${esc(it.who)}</span>${kindTag ? `<span class="tag">${kindTag}</span>` : ''}<span class="body">${body}</span></div></div>`;
}
function appendFeed(rs, from) {
  const box = $('#feed'); if (from === 0) box.innerHTML = '';
  const items = rs.feed.slice(from).filter(feedMatch);
  if (!items.length && from === 0) { box.innerHTML = '<div class="empty">No events for this filter yet.</div>'; return; }
  const empty = box.querySelector('.empty'); if (empty) empty.remove();
  box.insertAdjacentHTML('beforeend', items.map(feedItemHTML).join(''));
  while (box.children.length > 800) box.firstChild.remove();
  if ($('#autoScroll').checked) box.scrollTop = box.scrollHeight;
}

// ---- reports
function renderReports(rs) {
  const has = k => k === '_debate_research' ? rs.debate.some(d => d.team === 'research') : k === '_debate_risk' ? rs.debate.some(d => d.team === 'risk') : !!rs.reports[k];
  $('#rtabs').innerHTML = REPORTS.map(([k, l]) => `<button data-k="${k}" class="${k === S.reportTab ? 'active' : ''} ${has(k) ? 'has' : ''}"><span class="d"></span>${l}</button>`).join('');
  const k = S.reportTab; const body = $('#rbody');
  const key = k + ':' + (k.startsWith('_') ? rs.debate.length : (rs.reports[k] || '').length) + rs.id;
  if (body.dataset.key === key) return; body.dataset.key = key;
  if (k.startsWith('_debate')) {
    const team = k.endsWith('research') ? 'research' : 'risk';
    const items = rs.debate.filter(d => d.team === team);
    body.innerHTML = items.length ? `<div class="debate">${items.map(d => {
      const judge = ['Research Manager', 'Portfolio Manager'].includes(d.speaker);
      const col = d.speaker.startsWith('Bull') || d.speaker.startsWith('Aggressive') ? 'var(--good)' : d.speaker.startsWith('Bear') || d.speaker.startsWith('Conservative') ? 'var(--critical)' : `var(${TEAM_OF[d.speaker]?.color || '--neutral'})`;
      return `<div class="bubble ${judge ? 'judge' : ''}" style="--sw:${col}"><div class="who">${esc(d.speaker)}${judge ? '<span class="tag">verdict</span>' : ''}<span class="muted mono" style="font-weight:400;font-size:11px">${fmtTime(d.ts)}</span></div><div class="md" style="padding:0;max-height:none">${md(d.content)}</div></div>`;
    }).join('')}</div>` : '<div class="empty">This debate has not started yet.</div>';
  } else body.innerHTML = rs.reports[k] ? `<div class="md">${md(rs.reports[k])}</div>` : '<div class="empty">This report has not been written yet.</div>';
}
$('#rtabs').addEventListener('click', e => { const b = e.target.closest('button'); if (!b) return; S.reportTab = b.dataset.k; if (S.rs[S.focus]) renderReports(S.rs[S.focus]); });

// ------------------------------------------------------------------ charts
let priceChart, candle, sma20, sma50, bbU, bbL, indChart, indSeries = [], tokenChart, tokenLine, equityChart, equityArea;
function chartTheme() {
  return { layout: { background: { type: 'solid', color: cssv('--surface-1') }, textColor: cssv('--text-secondary'), fontFamily: 'Inter, system-ui', fontSize: 11 },
    grid: { vertLines: { color: cssv('--grid') }, horzLines: { color: cssv('--grid') } },
    rightPriceScale: { borderColor: cssv('--border') }, timeScale: { borderColor: cssv('--border') } };
}
function initCharts() {
  const LW = LightweightCharts;
  priceChart = LW.createChart($('#priceChart'), { ...chartTheme(), autoSize: true, crosshair: { mode: 0 }, timeScale: { timeVisible: true, secondsVisible: false, borderColor: cssv('--border') } });
  candle = priceChart.addCandlestickSeries({ upColor: '#0ca30c', downColor: '#d03b3b', wickUpColor: '#0ca30c', wickDownColor: '#d03b3b', borderVisible: false });
  sma20 = priceChart.addLineSeries({ color: cssv('--team-analyst'), lineWidth: 2, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
  sma50 = priceChart.addLineSeries({ color: cssv('--team-trader'), lineWidth: 2, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
  bbU = priceChart.addLineSeries({ color: cssv('--text-muted'), lineWidth: 1, lineStyle: 2, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
  bbL = priceChart.addLineSeries({ color: cssv('--text-muted'), lineWidth: 1, lineStyle: 2, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
  indChart = LW.createChart($('#indChart'), { ...chartTheme(), autoSize: true, timeScale: { visible: false } });
  priceChart.subscribeCrosshairMove(p => updateLegend(p && p.time));
  const sync = (a, b) => a.timeScale().subscribeVisibleLogicalRangeChange(r => { if (r) b.timeScale().setVisibleLogicalRange(r); });
  sync(priceChart, indChart); sync(indChart, priceChart);
  tokenChart = LW.createChart($('#tokenChart'), { ...chartTheme(), autoSize: true, handleScroll: false, handleScale: false,
    timeScale: { timeVisible: true, secondsVisible: true, borderColor: cssv('--border') }, rightPriceScale: { borderColor: cssv('--border') } });
  tokenLine = tokenChart.addAreaSeries({ lineColor: cssv('--accent'), topColor: 'rgba(57,135,229,.25)', bottomColor: 'rgba(57,135,229,0)', lineWidth: 2,
    priceFormat: { type: 'custom', formatter: v => fmtK(Math.round(v)) + ' tok' } });
  equityChart = LW.createChart($('#equityChart'), { ...chartTheme(), autoSize: true, timeScale: { timeVisible: true, borderColor: cssv('--border') } });
  equityArea = equityChart.addAreaSeries({ lineColor: cssv('--accent'), topColor: 'rgba(57,135,229,.25)', bottomColor: 'rgba(57,135,229,0)', lineWidth: 2 });
}
function retheme() { [priceChart, indChart, tokenChart, equityChart].forEach(c => c && c.applyOptions(chartTheme())); if (S.rs[S.focus]) renderTimeline(S.rs[S.focus]); }

function setTicker(t) {
  S.ticker = t.toUpperCase(); subscribe(); renderWatchlist(); loadPrices();
}
async function loadPrices() {
  const t = S.ticker, iv = S.interval;
  $('#chartTitle').textContent = t; $('#priceMsg').style.display = ''; $('#priceMsg').textContent = 'Loading ' + t + '…';
  try {
    const d = await api(`/api/prices/${encodeURIComponent(t)}?interval=${iv}`);
    if (t !== S.ticker || iv !== S.interval) return;
    S.bars = d.bars;
    candle.setData(d.bars.map(b => ({ time: b.time, open: b.open, high: b.high, low: b.low, close: b.close })));
    const line = k => d.bars.filter(b => b[k] != null).map(b => ({ time: b.time, value: b[k] }));
    const sm = S.overlays.has('sma'), bb = S.overlays.has('bb');
    sma20.setData(sm ? line('sma20') : []); sma50.setData(sm ? line('sma50') : []);
    bbU.setData(bb ? line('bb_up') : []); bbL.setData(bb ? line('bb_lo') : []);
    renderIndicator(); setMarkers(); priceChart.timeScale().fitContent();
    const n = d.bars.length; if (n > 150) priceChart.timeScale().setVisibleLogicalRange({ from: n - 150, to: n + 3 });
    $('#priceMsg').style.display = 'none'; updateLegend(null); liveQuote(S.quotes[t]);
  } catch (e) { $('#priceMsg').textContent = 'No price data: ' + e.message; }
}
function renderIndicator() {
  indSeries.forEach(s => indChart.removeSeries(s)); indSeries = [];
  const b = S.bars, p = S.indPane; const line = k => b.filter(x => x[k] != null).map(x => ({ time: x.time, value: x[k] }));
  if (p === 'rsi') {
    const s = indChart.addLineSeries({ color: cssv('--team-risk'), lineWidth: 2, priceLineVisible: false });
    s.setData(line('rsi'));
    [70, 30].forEach(v => s.createPriceLine({ price: v, color: cssv('--text-muted'), lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: '' }));
    indSeries.push(s); $('#indLegend').textContent = 'RSI(14) — 70 overbought · 30 oversold';
  } else if (p === 'macd') {
    const h = indChart.addHistogramSeries({ priceLineVisible: false, lastValueVisible: false });
    h.setData(b.filter(x => x.hist != null).map(x => ({ time: x.time, value: x.hist, color: x.hist >= 0 ? 'rgba(12,163,12,.55)' : 'rgba(208,59,59,.55)' })));
    const m = indChart.addLineSeries({ color: cssv('--team-analyst'), lineWidth: 2, priceLineVisible: false });
    const sg = indChart.addLineSeries({ color: cssv('--team-trader'), lineWidth: 2, priceLineVisible: false });
    m.setData(line('macd')); sg.setData(line('signal')); indSeries.push(h, m, sg);
    $('#indLegend').innerHTML = '<span><span class="sw" style="background:var(--team-analyst)"></span>MACD</span><span><span class="sw" style="background:var(--team-trader)"></span>Signal</span><span>bars: histogram</span>';
  } else {
    const h = indChart.addHistogramSeries({ priceFormat: { type: 'volume' }, priceLineVisible: false });
    h.setData(b.map(x => ({ time: x.time, value: x.volume, color: x.close >= x.open ? 'rgba(12,163,12,.5)' : 'rgba(208,59,59,.5)' })));
    indSeries.push(h); $('#indLegend').textContent = 'Volume';
  }
  const r = priceChart.timeScale().getVisibleLogicalRange(); if (r) indChart.timeScale().setVisibleLogicalRange(r);
}
function updateLegend(time) {
  const b = (time != null && S.bars.find(x => x.time === time)) || S.bars[S.bars.length - 1]; if (!b) return;
  const sw = v => `<span class="sw" style="background:var(${v})"></span>`;
  let h = `<span>O ${fmtN(b.open, 2)}</span><span>H ${fmtN(b.high, 2)}</span><span>L ${fmtN(b.low, 2)}</span><span>C ${fmtN(b.close, 2)}</span><span>Vol ${fmtK(b.volume)}</span>`;
  if (S.overlays.has('sma')) h += `<span>${sw('--team-analyst')}SMA20 ${fmtN(b.sma20, 2)}</span><span>${sw('--team-trader')}SMA50 ${fmtN(b.sma50, 2)}</span>`;
  if (S.overlays.has('bb')) h += `<span>${sw('--text-muted')}BB ${fmtN(b.bb_lo, 2)}–${fmtN(b.bb_up, 2)}</span>`;
  h += `<span>RSI ${fmtN(b.rsi, 1)}</span>`;
  if (S.overlays.has('marks')) h += '<span class="muted">▲ bullish · ▼ bearish · ● hold · B/S paper fills</span>';
  $('#priceLegend').innerHTML = h;
}
function barDate(t) { return typeof t === 'string' ? t : new Date(t * 1000).toISOString().slice(0, 10); }
function snapTime(date) {
  let best = null; for (const b of S.bars) { if (barDate(b.time) <= date) best = b.time; else break; } return best;
}
function setMarkers() {
  if (!candle) return;
  if (!S.overlays.has('marks') || !S.bars.length) { candle.setMarkers([]); return; }
  const good = '#0ca30c', bad = '#d03b3b', neu = cssv('--text-muted');
  const m = [];
  Object.values(S.runs).filter(r => r.ticker === S.ticker && r.status === 'done' && r.signal && r.signal !== 'REVIEW').forEach(r => {
    const t = snapTime(r.date); if (t == null) return;
    const up = BULLISH.includes(r.signal), dn = BEARISH.includes(r.signal);
    m.push({ time: t, position: dn ? 'aboveBar' : 'belowBar', color: up ? good : dn ? bad : neu, shape: up ? 'arrowUp' : dn ? 'arrowDown' : 'circle', text: r.signal + (r.simulated ? ' (sim)' : '') });
  });
  (S.portfolio?.trades || []).filter(t => t.ticker === S.ticker && t.side !== 'NONE').forEach(t => {
    const tt = snapTime(t.date); if (tt == null) return;
    m.push({ time: tt, position: t.side === 'SELL' ? 'aboveBar' : 'belowBar', color: t.side === 'BUY' ? good : bad, shape: 'square', text: `${t.side[0]} ${fmtQ(t.qty)} @ ${fmtN(t.price, 2)}` });
  });
  m.sort((a, b) => (a.time > b.time) - (a.time < b.time));
  try { candle.setMarkers(m); } catch (e) { console.warn(e); }
}
function liveQuote(q) {
  if (!q || q.price == null) return;
  const up = (q.change_pct || 0) >= 0;
  $('#chartQuote').innerHTML = `${fmtN(q.price, 2)} <span class="chg ${up ? 'up' : 'down'}">${up ? '▲' : '▼'} ${fmtN(q.change_pct, 2)}%</span>`;
  if (S.interval === '1d' && S.bars.length && candle) {
    const last = S.bars[S.bars.length - 1];
    if (last.time === today()) { last.close = q.price; last.high = Math.max(last.high, q.price); last.low = Math.min(last.low, q.price);
      try { candle.update({ time: last.time, open: last.open, high: last.high, low: last.low, close: last.close }); } catch (e) {} }
  }
}
function renderTokenChart(rs) {
  const pts = []; let lastT = -1;
  rs.tokens.forEach(([ts, v]) => { const t = Math.floor(ts); if (t === lastT) pts[pts.length - 1].value = v; else { pts.push({ time: t, value: v }); lastT = t; } });
  if (rs.t0 && (!pts.length || pts[0].time > Math.floor(rs.t0))) pts.unshift({ time: Math.floor(rs.t0), value: 0 });
  tokenLine.setData(pts); tokenChart.timeScale().fitContent();
}

// ------------------------------------------------------------------ watchlist & queue
function renderWatchlist() {
  const wl = S.settings?.watchlist || [];
  $('#watchlist').innerHTML = wl.map(t => `<div class="wl-row ${t === S.ticker ? 'sel' : ''}" data-t="${esc(t)}"><span class="t">${esc(t)}</span><span class="mono" id="wq-${cssId(t)}">—</span><button class="btn small" data-run="${esc(t)}" title="Analyze ${esc(t)}">▶</button></div>`).join('') || '<div class="empty">Empty</div>';
  wl.forEach(renderWatchQuote);
}
const cssId = t => t.replace(/[^A-Za-z0-9]/g, '_');
function renderWatchQuote(t) {
  const el = document.getElementById('wq-' + cssId(t)); const q = S.quotes[t]; if (!el || !q || q.price == null) return;
  const up = (q.change_pct || 0) >= 0;
  el.innerHTML = `${fmtN(q.price, 2)} <span class="chg ${up ? 'up' : 'down'}">${up ? '+' : ''}${fmtN(q.change_pct, 2)}%</span>`;
}
$('#watchlist').addEventListener('click', e => {
  const r = e.target.closest('[data-run]'); if (r) { e.stopPropagation(); startRuns([r.dataset.run]); return; }
  const row = e.target.closest('.wl-row'); if (row) setTicker(row.dataset.t);
});
function renderQueue() {
  const runs = Object.values(S.runs).sort((a, b) => b.created - a.created).slice(0, 40);
  const active = runs.filter(r => ['queued', 'running'].includes(r.status)).length;
  $('#queueCount').textContent = active ? `${active} active` : '';
  const cur = Object.values(S.runs).find(r => r.status === 'running');
  $('#engDot').className = 'dot ' + (cur ? 'run' : '');
  $('#engText').textContent = cur ? `Running ${cur.ticker}` + (active > 1 ? ` · ${active - 1} queued` : '') : 'Idle';
  $('#queue').innerHTML = runs.map(r => `<div class="q-row" data-id="${r.id}" style="${r.id === S.focus ? 'background:var(--surface-2)' : ''}">
    <b>${esc(r.ticker)}</b><span class="muted mono" style="font-size:11px">${esc(r.date.slice(5))}</span><span style="flex:1"></span>
    ${r.status === 'done' ? ratingBadge(r.signal) : statusBadge(r.status)}${r.simulated ? '<span class="badge sim" title="Simulated">S</span>' : ''}
    ${['queued', 'running'].includes(r.status) ? `<button class="btn small danger" data-cancel="${r.id}" title="Cancel">✕</button>` : ''}</div>`).join('') || '<div class="empty">No runs yet.</div>';
  renderHistory();
}
$('#queue').addEventListener('click', e => {
  const c = e.target.closest('[data-cancel]'); if (c) { e.stopPropagation(); api(`/api/runs/${c.dataset.cancel}/cancel`, { method: 'POST' }); return; }
  const r = e.target.closest('.q-row'); if (r) { S.follow = false; focusRun(r.dataset.id); }
});

async function startRuns(tickers, date) {
  tickers = tickers.map(t => t.trim().toUpperCase()).filter(Boolean);
  if (!tickers.length) { toast('Enter at least one ticker.'); return; }
  try { await api('/api/runs', { method: 'POST', body: { tickers, date: date || $('#runDate').value || null } }); S.follow = true;
    toast(`Queued ${tickers.map(esc).join(', ')}`); }
  catch (e) { toast('Could not start: ' + esc(e.message), 7000); }
}

// ------------------------------------------------------------------ portfolio
async function loadPortfolio() { try { S.portfolio = await api('/api/portfolio'); renderPortfolio(); } catch (e) {} }
function renderPortfolio() {
  const p = S.portfolio; if (!p) return;
  const c = p.currency || 'INR';
  $('#pEquity').textContent = fmtN(p.equity, 2) + ' ' + c;
  $('#pReturn').innerHTML = `<span class="chg ${p.return_pct >= 0 ? 'up' : 'down'}">${p.return_pct >= 0 ? '+' : ''}${fmtN(p.return_pct, 2)}%</span>`;
  $('#pCash').textContent = fmtN(p.cash, 2); $('#pMV').textContent = fmtN(p.market_value, 2);
  $('#pAuto').checked = !!p.auto_execute; $('#pAware').checked = !!p.portfolio_aware; $('#pMax').value = Math.round(p.max_weight * 100);
  $('#posTable').innerHTML = '<tr><th>Ticker</th><th class="r">Qty</th><th class="r">Avg</th><th class="r">Last</th><th class="r">Value</th><th class="r">P&amp;L</th></tr>' +
    (p.positions.map(x => `<tr class="click" onclick="setTicker('${esc(x.ticker)}');showView('live')"><td><b>${esc(x.ticker)}</b></td><td class="r num">${fmtQ(x.qty)}</td><td class="r num">${fmtN(x.avg_price, 2)}</td><td class="r num">${fmtN(x.price, 2)}</td><td class="r num">${fmtN(x.value, 2)}</td><td class="r num chg ${x.pnl >= 0 ? 'up' : 'down'}">${x.pnl >= 0 ? '+' : ''}${fmtN(x.pnl, 2)} (${fmtN(x.pnl_pct, 1)}%)</td></tr>`).join('') || '<tr><td colspan="6" class="muted">Flat — no positions.</td></tr>');
  $('#tradeTable').innerHTML = '<tr><th>Analysis date</th><th>Ticker</th><th>Rating</th><th>Side</th><th class="r">Qty</th><th class="r">Price</th><th class="r">Realised</th><th>Run</th></tr>' +
    (p.trades.map(t => `<tr><td class="mono">${esc(t.date)}</td><td><b>${esc(t.ticker)}</b></td><td>${ratingBadge(t.rating)}</td><td>${t.side === 'NONE' ? '<span class="muted">none</span>' : `<span class="chg ${t.side === 'BUY' ? 'up' : 'down'}">${t.side}</span>`}</td><td class="r num">${fmtQ(t.qty)}</td><td class="r num">${fmtN(t.price, 2)}</td><td class="r num">${t.realized_pnl ? fmtN(t.realized_pnl, 2) : ''}</td><td>${t.run_id && t.run_id !== 'manual' ? `<a href="#" onclick="focusRun('${t.run_id}');return false">open</a>` : esc(t.run_id)}</td></tr>`).join('') || '<tr><td colspan="8" class="muted">No trades yet.</td></tr>');
  const pts = []; let lt = -1; (p.equity_curve || []).forEach(e => { if (e.time > lt) { pts.push({ time: e.time, value: e.equity }); lt = e.time; } });
  equityArea.setData(pts); $('#equityMsg').style.display = pts.length ? 'none' : ''; if (pts.length) equityChart.timeScale().fitContent();
  setMarkers();
}
$('#pSave').onclick = async () => { S.portfolio = await api('/api/portfolio', { method: 'POST', body: { auto_execute: $('#pAuto').checked, portfolio_aware: $('#pAware').checked, max_weight: Math.max(0.01, Math.min(1, $('#pMax').value / 100)) } }); renderPortfolio(); toast('Execution rules saved'); };
$('#pReset').onclick = async () => { if (!confirm('Reset the paper ledger? All paper positions and trades are cleared.')) return; S.portfolio = await api('/api/portfolio', { method: 'POST', body: { reset_cash: +$('#pResetCash').value } }); renderPortfolio(); };
$('#mApply').onclick = async () => { try { const r = await api('/api/portfolio/apply', { method: 'POST', body: { ticker: $('#mTicker').value, rating: $('#mRating').value } }); toast(esc(tradeText(r.trade)) || 'No price available'); } catch (e) { toast(esc(e.message)); } };

// ------------------------------------------------------------------ history
function renderHistory() {
  const f = ($('#histFilter').value || '').toUpperCase();
  const runs = Object.values(S.runs).filter(r => !f || r.ticker.includes(f)).sort((a, b) => b.created - a.created);
  $('#runTable').innerHTML = '<tr><th>Created</th><th>Ticker</th><th>Analysis date</th><th>Status</th><th>Rating</th><th>Provider</th><th class="r">LLM calls</th><th class="r">Tokens</th><th class="r">Duration</th><th>Paper trade</th><th></th></tr>' +
    (runs.map(r => `<tr class="click" data-id="${r.id}"><td class="mono">${new Date(r.created * 1000).toLocaleString()}</td><td><b>${esc(r.ticker)}</b></td><td class="mono">${esc(r.date)}</td><td>${statusBadge(r.status)}</td><td>${ratingBadge(r.signal)}</td><td>${esc(r.provider)}${r.simulated ? ' <span class="badge sim">SIM</span>' : ''}</td><td class="r num">${fmtN(r.stats?.llm_calls)}</td><td class="r num">${r.stats ? fmtK(r.stats.tokens_in + r.stats.tokens_out) : '—'}</td><td class="r num">${r.finished && r.started ? fmtDur(r.finished - r.started) : '—'}</td><td>${r.trade ? esc(tradeText(r.trade)) : ''}</td><td>${['queued', 'running'].includes(r.status) ? '' : `<button class="btn small" data-del="${r.id}" title="Delete">✕</button>`}</td></tr>`).join('') || '<tr><td colspan="11" class="muted">No runs yet.</td></tr>');
}
$('#runTable').addEventListener('click', async e => {
  const d = e.target.closest('[data-del]'); if (d) { e.stopPropagation(); await api('/api/runs/' + d.dataset.del, { method: 'DELETE' }); delete S.runs[d.dataset.del]; renderQueue(); return; }
  const r = e.target.closest('tr[data-id]'); if (r) { S.follow = false; focusRun(r.dataset.id); }
});
$('#histFilter').oninput = renderHistory;
async function loadDecisions() {
  try {
    const rows = await api('/api/decisions');
    $('#decTable').innerHTML = '<tr><th>Date</th><th>Ticker</th><th>Rating</th><th>Status</th><th class="r">Return</th><th class="r">Alpha</th><th>Holding</th><th>Reflection &amp; decision</th></tr>' +
      (rows.map(e => `<tr><td class="mono">${esc(e.date)}</td><td><b>${esc(e.ticker)}</b></td><td>${ratingBadge(e.rating)}</td><td>${e.pending ? '<span class="badge st-queued">pending</span>' : '<span class="badge st-done">settled</span>'}</td><td class="r num">${esc(e.raw || '')}</td><td class="r num">${esc(e.alpha || '')}</td><td>${esc(e.holding || '')}</td><td><details><summary class="sec">${esc((e.reflection || e.decision || '').slice(0, 120))}…</summary><div class="md" style="max-height:320px">${e.reflection ? '<b>Reflection</b>' + md(e.reflection) : ''}<b>Decision</b>${md(e.decision)}</div></details></td></tr>`).join('') || '<tr><td colspan="8" class="muted">The decision log is empty. Each finished run is appended; outcomes settle on the next run for the same ticker.</td></tr>');
  } catch (e) {}
}
$('#decRefresh').onclick = loadDecisions;

// ------------------------------------------------------------------ backtest
function btEstimate() {
  const t = $('#btTickers').value.split(',').filter(x => x.trim()).length, s = $('#btStart').value, e = $('#btEnd').value, n = +$('#btEvery').value || 1;
  if (!t || !s || !e) { $('#btEstimate').textContent = ''; return; }
  const days = Math.max(0, (new Date(e) - new Date(s)) / 864e5); const cells = t * (Math.floor(days / n) + 1);
  $('#btEstimate').textContent = `≈ ${cells} runs (${cells} full pipeline executions — mind your API costs).`;
}
['#btTickers', '#btStart', '#btEnd', '#btEvery'].forEach(s => $(s).addEventListener('input', btEstimate));
$('#btRun').onclick = async () => {
  try { const b = await api('/api/backtest', { method: 'POST', body: { tickers: $('#btTickers').value.split(','), start: $('#btStart').value, end: $('#btEnd').value, every: +$('#btEvery').value } });
    S.batches[b.id] = b; renderBacktests(); toast(`Backtest queued: ${b.total} runs`); } catch (e) { toast('Backtest: ' + esc(e.message), 7000); }
};
function renderBacktests() {
  const bs = Object.values(S.batches).sort((a, b) => b.created - a.created);
  $('#btList').innerHTML = bs.map(b => {
    const cols = b.dates.length;
    return `<div class="card"><div class="card-h"><h3>${esc(b.id)}</h3><span class="sec">${esc(b.tickers.join(', '))} · ${esc(b.dates[0] || '')} → ${esc(b.dates[b.dates.length - 1] || '')}</span><span class="spacer"></span>${statusBadge(b.status)}<span class="mono">${b.done}/${b.total}</span></div>
      <div class="card-b"><div class="cellgrid" style="grid-template-columns:70px repeat(${cols}, minmax(38px,1fr))"><div></div>${b.dates.map(d => `<div class="muted" style="text-align:center">${d.slice(5)}</div>`).join('')}
      ${b.tickers.map(t => `<div><b>${esc(t)}</b></div>` + b.dates.map(d => { const c = b.cells.find(x => x.ticker === t && x.date === d) || {}; return `<div class="cell ${esc((c.signal || '').toLowerCase())} ${c.status === 'running' ? 'running' : ''}" title="${esc(t)} ${d}: ${esc(c.signal || c.status || '')}" onclick="${c.id ? `focusRun('${c.id}')` : ''}">${esc(c.signal ? c.signal.slice(0, 4) : c.status === 'running' ? '…' : c.status === 'failed' ? 'err' : '')}</div>`; }).join('')).join('')}</div>
      ${b.summary ? `<pre class="mono" style="white-space:pre-wrap;margin-top:10px">${esc(b.summary.text)}</pre>` : ''}</div></div>`;
  }).join('');
  const done = bs.find(b => b.summary && b.summary.by_rating && Object.keys(b.summary.by_rating).length);
  if (done) renderBtChart(done.summary.by_rating);
}
function renderBtChart(by) {
  const rows = ['Buy', 'Overweight', 'Hold', 'Underweight', 'Sell'].filter(r => by[r]);
  const max = Math.max(0.005, ...rows.map(r => Math.abs(by[r].mean_alpha)));
  $('#btChart').innerHTML = `<div class="hint" style="margin-bottom:8px">Mean alpha vs benchmark per rating (bars) · n = cells · hit = direction called correctly</div>` +
    rows.map(r => { const v = by[r].mean_alpha, w = Math.abs(v) / max * 50, hit = by[r].hit_rate;
      const tip = `${r}: n=${by[r].count}\nmean alpha ${(v * 100).toFixed(2)}%\nhit rate ${hit == null ? 'n/a (Hold claims no direction)' : Math.round(hit * 100) + '%'}`;
      return `<div style="display:grid;grid-template-columns:90px 1fr 150px;gap:8px;align-items:center;margin:6px 0">
        <span>${ratingBadge(r)}</span>
        <div style="position:relative;height:18px;border-left:1px solid transparent"><div style="position:absolute;left:50%;top:-3px;bottom:-3px;border-left:1px solid var(--border-strong)"></div>
          <div data-tip="${esc(tip)}" style="position:absolute;top:2px;height:14px;border-radius:3px;${v >= 0 ? 'left:50%' : `right:50%`};width:${Math.max(0.5, w)}%;background:${v >= 0 ? '#0ca30c' : '#d03b3b'}"></div></div>
        <span class="mono" style="font-size:12px">${v >= 0 ? '+' : ''}${(v * 100).toFixed(2)}% · n=${by[r].count}${hit != null ? ' · hit ' + Math.round(hit * 100) + '%' : ''}</span></div>`; }).join('');
}

// ------------------------------------------------------------------ settings
function renderProviderPill() {
  const s = S.settings; if (!s) return;
  $('#providerPill').innerHTML = `<b>${esc(s.llm_provider)}</b>&nbsp;<span class="muted">${esc(s.quick_think_llm)} / ${esc(s.deep_think_llm)}</span>`;
  $('#simBanner').style.display = (S.rs[S.focus]?.summary.simulated ?? s.llm_provider === 'simulation') ? '' : 'none';
}
function fillModels(sel, custom, list, val) {
  sel.innerHTML = list.map(([l, v]) => `<option value="${esc(v)}">${esc(l)}</option>`).join('');
  const known = list.some(([, v]) => v === val);
  sel.value = known ? val : 'custom'; if (!known && ![...sel.options].some(o => o.value === 'custom')) sel.insertAdjacentHTML('beforeend', '<option value="custom">Custom model ID</option>'), sel.value = 'custom';
  custom.style.display = sel.value === 'custom' ? '' : 'none'; custom.value = known ? '' : (val || '');
}
function renderSettings() {
  const s = S.settings, m = S.meta;
  $('#sProvider').innerHTML = m.providers.map(p => `<option value="${p.key}">${esc(p.label)}</option>`).join('');
  $('#sProvider').value = s.llm_provider; onProvider(true);
  $('#sUrl').value = s.backend_url || ''; $('#sTemp').value = s.temperature ?? ''; $('#sLang').value = s.output_language || 'English';
  $('#sEffort').value = s.openai_reasoning_effort || s.anthropic_effort || s.google_thinking_level || '';
  $('#sAnalysts').innerHTML = [['market', 'Market / technical'], ['social', 'Sentiment'], ['news', 'News & macro'], ['fundamentals', 'Fundamentals']].map(([k, l]) => `<label class="chk"><input type="checkbox" value="${k}" ${s.analysts.includes(k) ? 'checked' : ''}>${l}</label>`).join('');
  $('#sDepth').value = String(s.research_depth); $('#sCkpt').checked = !!s.checkpoint_enabled; $('#sSimDelay').value = s.sim_delay;
  $('#sVendors').innerHTML = Object.entries(m.vendor_options).map(([k, opts]) => `<label>${k.replace(/_/g, ' ')}</label><select data-v="${k}">${[...new Set([s.data_vendors[k], ...opts])].map(o => `<option ${o === s.data_vendors[k] ? 'selected' : ''}>${esc(o)}</option>`).join('')}</select>`).join('') +
    '<div class="full hint">Comma = ordered fallback. Alpha Vantage needs ALPHA_VANTAGE_API_KEY; FRED needs FRED_API_KEY (macro is optional); SEC EDGAR gives point-in-time US filings.</div>';
  $('#sWatch').value = s.watchlist.join(', ');
  $('#sSchedOn').checked = !!s.schedule.enabled; $('#sSchedTimes').value = (s.schedule.times || []).join(', '); $('#sSchedNews').checked = s.schedule.first_slot_news !== false; $('#sWeekly').checked = !!(s.schedule.weekly_full || {}).enabled; $('#sSchedWk').checked = s.schedule.weekdays_only !== false;
  $('#envPath').textContent = m.env_path; $('#memPath').textContent = m.memory_log;
  renderKeys();
}
function onProvider(initial) {
  const p = S.meta.providers.find(x => x.key === $('#sProvider').value) || S.meta.providers[0];
  const s = S.settings;
  const q = initial ? s.quick_think_llm : p.models.quick[0]?.[1], d = initial ? s.deep_think_llm : p.models.deep[0]?.[1];
  fillModels($('#sQuick'), $('#sQuickCustom'), p.models.quick, q); fillModels($('#sDeep'), $('#sDeepCustom'), p.models.deep, d);
  renderKeys();
}
$('#sProvider').onchange = () => { onProvider(false); $('#sUrl').value = ''; };
$('#sQuick').onchange = () => $('#sQuickCustom').style.display = $('#sQuick').value === 'custom' ? '' : 'none';
$('#sDeep').onchange = () => $('#sDeepCustom').style.display = $('#sDeep').value === 'custom' ? '' : 'none';
function renderKeys() {
  const need = S.meta.providers.find(x => x.key === $('#sProvider').value)?.env;
  $('#keys').innerHTML = S.meta.keys.map(k => `<div class="keyrow"><span class="mono" style="${k.name === need ? 'font-weight:600' : ''}">${esc(k.name)}${k.name === need ? ' <span class="badge st-running">needed</span>' : ''}</span>
    <input type="password" placeholder="${k.set ? esc(k.masked) : 'not set'}" data-k="${esc(k.name)}" autocomplete="off">
    <button class="btn small" data-save="${esc(k.name)}">Save</button>${k.set ? `<button class="btn small danger" data-clear="${esc(k.name)}">Clear</button>` : '<span class="muted" style="font-size:11px">unset</span>'}</div>`).join('');
}
$('#keys').addEventListener('click', async e => {
  const b = e.target.closest('[data-save],[data-clear]'); if (!b) return;
  const name = b.dataset.save || b.dataset.clear; const value = b.dataset.save ? $(`input[data-k="${name}"]`).value : '';
  if (b.dataset.save && !value) return;
  const r = await api('/api/keys', { method: 'POST', body: { name, value } }); S.meta.keys = r.keys; renderKeys(); toast(`${esc(name)} ${value ? 'saved' : 'cleared'}`);
});
$('#sSave').onclick = async () => {
  const prov = $('#sProvider').value, eff = $('#sEffort').value;
  const pick = (sel, cus) => sel.value === 'custom' ? cus.value.trim() : sel.value;
  const body = {
    llm_provider: prov, quick_think_llm: pick($('#sQuick'), $('#sQuickCustom')), deep_think_llm: pick($('#sDeep'), $('#sDeepCustom')),
    backend_url: $('#sUrl').value.trim(), temperature: $('#sTemp').value, output_language: $('#sLang').value || 'English',
    openai_reasoning_effort: ['openai', 'azure'].includes(prov) ? eff : '', anthropic_effort: prov === 'anthropic' ? eff : '',
    google_thinking_level: prov === 'google' ? (eff === 'low' ? 'minimal' : eff) : '',
    analysts: $$('#sAnalysts input:checked').map(i => i.value), research_depth: +$('#sDepth').value, checkpoint_enabled: $('#sCkpt').checked,
    sim_delay: +$('#sSimDelay').value || 0, data_vendors: Object.fromEntries($$('#sVendors select').map(s => [s.dataset.v, s.value])),
    watchlist: $('#sWatch').value.split(',').map(t => t.trim().toUpperCase()).filter(Boolean),
    schedule: { ...(S.settings.schedule || {}), enabled: $('#sSchedOn').checked, times: $('#sSchedTimes').value.split(',').map(t => t.trim()).filter(t => /^\d\d:\d\d$/.test(t)), first_slot_news: $('#sSchedNews').checked, weekly_full: { ...((S.settings.schedule || {}).weekly_full || {}), enabled: $('#sWeekly').checked }, weekdays_only: $('#sSchedWk').checked },
  };
  if (!body.analysts.length) { toast('Pick at least one analyst.'); return; }
  if (!body.quick_think_llm || !body.deep_think_llm) { toast('Enter a model id.'); return; }
  S.settings = await api('/api/settings', { method: 'POST', body }); renderProviderPill(); renderWatchlist();
  $('#sSaved').textContent = 'Saved ' + new Date().toLocaleTimeString();
};

// ------------------------------------------------------------------ nav & controls
function showView(v) {
  $$('#tabs button').forEach(b => b.classList.toggle('active', b.dataset.view === v));
  $$('.view').forEach(s => s.classList.toggle('active', s.id === 'view-' + v));
  if (v === 'portfolio') loadPortfolio();
  if (v === 'history') { renderHistory(); loadDecisions(); }
  if (v === 'backtest') api('/api/backtest').then(l => { l.forEach(b => S.batches[b.id] = b); renderBacktests(); });
  try { localStorage.setItem('ta-view', v); } catch (e) {}
}
$('#tabs').onclick = e => { const b = e.target.closest('button'); if (b) showView(b.dataset.view); };
$('#runBtn').onclick = () => startRuns($('#runTickers').value.split(/[,\s]+/));
$('#runTickers').onkeydown = e => { if (e.key === 'Enter') $('#runBtn').click(); };
$('#runWatchBtn').onclick = () => startRuns(S.settings.watchlist);
$('#stopBtn').onclick = async () => { await api('/api/runs/cancel_all', { method: 'POST' }); toast('Cancelling — the current LLM call finishes first.'); };
$('#cancelRunBtn').onclick = () => S.focus && api(`/api/runs/${S.focus}/cancel`, { method: 'POST' });
$('#wlAddBtn').onclick = async () => { const t = $('#wlAdd').value.trim().toUpperCase(); if (!t) return;
  S.settings = await api('/api/settings', { method: 'POST', body: { watchlist: [...new Set([...S.settings.watchlist, t])] } }); $('#wlAdd').value = ''; renderWatchlist(); };
$('#themeBtn').onclick = () => { const cur = document.documentElement.dataset.theme === 'light' ? 'dark' : 'light'; document.documentElement.dataset.theme = cur; try { localStorage.setItem('ta-theme', cur); } catch (e) {} retheme(); };
$('#intervalSeg').onclick = e => { const b = e.target.closest('button'); if (!b) return; $$('#intervalSeg button').forEach(x => x.classList.toggle('active', x === b)); S.interval = b.dataset.i; loadPrices(); };
$('#overlaySeg').onclick = e => { const b = e.target.closest('button'); if (!b) return; const o = b.dataset.o; S.overlays.has(o) ? S.overlays.delete(o) : S.overlays.add(o); b.classList.toggle('active'); loadPrices(); };
$('#indSeg').onclick = e => { const b = e.target.closest('button'); if (!b) return; $$('#indSeg button').forEach(x => x.classList.toggle('active', x === b)); S.indPane = b.dataset.p; renderIndicator(); };
$('#feedSeg').onclick = e => { const b = e.target.closest('button'); if (!b) return; $$('#feedSeg button').forEach(x => x.classList.toggle('active', x === b)); S.feedFilter = b.dataset.f; if (S.rs[S.focus]) appendFeed(S.rs[S.focus], 0); };
window.addEventListener('resize', () => S.rs[S.focus] && renderTimeline(S.rs[S.focus]));
setInterval(() => { const rs = S.rs[S.focus]; if (rs && rs.summary.status === 'running') { renderTiles(rs); renderTimeline(rs); renderPipeline(rs); } }, 1000);

// ------------------------------------------------------------------ boot
(async function boot() {
  $('#runDate').value = today(); $('#runDate').max = today(); $('#btEnd').value = today();
  const s = new Date(); s.setMonth(s.getMonth() - 2); $('#btStart').value = s.toISOString().slice(0, 10);
  initCharts();
  S.meta = await api('/api/meta'); S.settings = S.meta.settings;
  renderSettings(); renderProviderPill();
  S.ticker = S.settings.watchlist[0] || 'NVDA'; renderWatchlist();
  const r = await api('/api/runs'); r.runs.forEach(x => S.runs[x.id] = x); S.current = r.current;
  await loadPortfolio();
  loadPrices(); renderQueue(); connect();
  api('/api/quotes?tickers=' + encodeURIComponent(S.settings.watchlist.join(','))).then(qs => qs.forEach(q => { S.quotes[q.ticker] = q; renderWatchQuote(q.ticker); if (q.ticker === S.ticker) liveQuote(q); }));
  const first = r.current || r.runs.find(x => x.status === 'done')?.id; if (first) focusRun(first, false);
  let v = 'live'; try { v = localStorage.getItem('ta-view') || 'live'; } catch (e) {} showView(v);
})();
