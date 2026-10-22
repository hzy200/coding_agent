const log = document.getElementById('log');
const promptEl = document.getElementById('prompt');
const sendBtn = document.getElementById('send');
const healthEl = document.getElementById('health');
const threadEl = document.getElementById('thread');
const sessionsEl = document.getElementById('sessions');
const stateEl = document.getElementById('state');
const steerActionEl = document.getElementById('steer-action');
const steerRoleEl = document.getElementById('steer-role');
const steerTextEl = document.getElementById('steer-text');
const loginEl = document.getElementById('login');
const tokenInputEl = document.getElementById('token-input');
const loginErrorEl = document.getElementById('login-error');
const logoutBtn = document.getElementById('logout');

const TOKEN_KEY = 'codingagent.token';
let threadId = '';
let busy = false;
let pendingApprovals = [];

function getToken() {
  return sessionStorage.getItem(TOKEN_KEY) || '';
}

function setToken(value) {
  if (value) sessionStorage.setItem(TOKEN_KEY, value);
  else sessionStorage.removeItem(TOKEN_KEY);
}

function showLogin(message) {
  loginEl.classList.remove('hidden');
  loginErrorEl.textContent = message || '';
  tokenInputEl.focus();
}

function hideLogin() {
  loginEl.classList.add('hidden');
  loginErrorEl.textContent = '';
}

async function authFetch(url, options = {}) {
  const headers = new Headers(options.headers || {});
  const token = getToken();
  if (token) headers.set('Authorization', `Bearer ${token}`);
  const response = await fetch(url, { ...options, headers });
  if (response.status === 401) {
    showLogin('token 无效或已过期');
  }
  return response;
}

function line(text, cls) {
  const div = document.createElement('div');
  if (cls) div.className = cls;
  div.textContent = text;
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
  return div;
}

function setBusy(value) {
  busy = value;
  sendBtn.disabled = value;
  promptEl.disabled = value;
}

async function health() {
  try {
    const r = await authFetch('/api/health');
    if (r.status === 401) {
      healthEl.textContent = '需要登录';
      healthEl.className = 'meta bad';
      return;
    }
    const d = await r.json();
    if (d.reachable) {
      healthEl.textContent = `API 已联通 · ${d.model} · ${d.user || ''}`;
      healthEl.className = 'meta ok';
    } else {
      healthEl.textContent = `API 不可用：${d.error || '未配置 Key'}`;
      healthEl.className = 'meta bad';
    }
  } catch (e) {
    healthEl.textContent = 'API 状态获取失败';
    healthEl.className = 'meta bad';
  }
}

async function loadSessions() {
  const r = await authFetch('/api/sessions');
  if (!r.ok) return;
  const d = await r.json();
  sessionsEl.replaceChildren();
  for (const s of d.sessions) {
    const li = document.createElement('li');
    if (s.thread_id === threadId) li.className = 'active';
    const t = document.createElement('span');
    t.className = 't';
    t.textContent = s.title || '（无标题）';
    const m = document.createElement('span');
    m.className = 'm';
    m.textContent = `${s.thread_id} · ${s.last_active} · ${s.prompts} 次提问`;
    li.append(t, m);
    li.addEventListener('click', () => openThread(s.thread_id));
    sessionsEl.appendChild(li);
  }
}

async function openThread(id) {
  if (busy) return;
  threadId = id;
  threadEl.textContent = `会话 ${id}`;
  log.replaceChildren();
  await loadSessions();
  try {
    const r = await authFetch(`/api/thread/${id}`);
    if (!r.ok) return;
    const d = await r.json();
    if (!d.messages.length) { line('（该会话没有可显示的历史）', 'tool'); return; }
    for (const m of d.messages) {
      line((m.role === 'user' ? '你 › ' : '助手 › ') + m.text,
           m.role === 'user' ? 'you' : 'say');
    }
  } catch (e) { /* 历史拿不到不影响继续对话 */ }
}

function render(ev) {
  switch (ev.type) {
    case 'plan_created':
      if (ev.steps.length) line('计划：\n' + ev.steps.map((s, i) => `  ${i + 1}. ${s}`).join('\n'), 'tool');
      break;
    case 'step_started':
      if (ev.total > 1) line(`第 ${ev.index + 1}/${ev.total} 步  ${ev.text}`, 'tool');
      break;
    case 'assistant_token':
      if (ev.node === 'respond' && !window._answered) {
        line('── 最终答复 ──', 'sep'); window._answered = true;
      }
      if (window._cur && window._cur.dataset.node === ev.node) {
        window._cur.textContent += ev.text;
      } else {
        window._cur = line(ev.text, 'say');
        window._cur.dataset.node = ev.node;
      }
      log.scrollTop = log.scrollHeight;
      break;
    case 'tool_call_started':
      window._cur = null;
      line(`→ ${ev.level ? '[' + ev.level + '] ' : ''}${ev.name}: ${ev.summary}`, 'tool');
      break;
    case 'tool_call_finished':
      window._cur = null;
      if (ev.rejected) line('← 已被安全策略拒绝', 'bad');
      else if (ev.skipped) line('← 已跳过（环境未配置）', 'tool');
      else if (ev.interrupted) line('← 结果未知（执行中断，未自动重放）', 'warn');
      else if (ev.ok) line('← ✓', 'tool');
      else line(`← ✗ exit=${ev.exit_code}`, 'warn');
      break;
    case 'file_changed':
      line(`✎ ${ev.action} ${ev.path}  +${ev.added} -${ev.removed}`, 'warn');
      break;
    case 'verification':
      if (ev.status === 'ok') line(`验证通过  ${ev.summary}`, 'ok');
      else if (ev.status === 'failed') {
        line(`验证失败  ${ev.summary}`, 'bad');
        for (const i of ev.issues.slice(0, 5)) line('  · ' + i, 'bad');
      }
      break;
    case 'review_finished':
      if (ev.blocked) line(`审查阻断  ${ev.summary}`, 'bad');
      else if (ev.status === 'warned') line(`审查告警  ${ev.summary}`, 'warn');
      break;
    case 'repair_started':
      line(`↻ 第 ${ev.attempt}/${ev.limit} 次修复  ${ev.summary}`, 'warn');
      break;
    case 'approval_requested':
      window._cur = null;
      pendingApprovals.push(ev);
      line(`⚠ 需要确认 [${ev.level || ''}] ${ev.tool}: ${ev.command}`, 'warn');
      break;
    case 'steering_applied':
      if (ev.action === 'pause') {
        stateEl.textContent = '已暂停';
        line('⏸ 已暂停；点击“恢复”继续', 'warn');
      } else {
        line(`steering: ${ev.action}${ev.detail ? ' ' + ev.detail : ''}`, 'tool');
      }
      break;
    case 'cost_report': {
      let text = `成本 $${ev.cost_usd.toFixed(4)}`;
      if (ev.budget_usd > 0) {
        text += ` / 预算 $${ev.budget_usd.toFixed(2)}（${Math.round(ev.ratio * 100)}%）`;
      }
      if (ev.billed_usd) {
        text += ` / 账单 $${ev.billed_usd.toFixed(4)}（差 $${ev.billing_delta_usd.toFixed(4)}）`;
      }
      line(text, 'tool');
      if (ev.billing_error) line('账单对账失败：' + ev.billing_error, 'warn');
      for (const alert of ev.alerts || []) line('⚠ ' + alert, 'warn');
      break;
    }
    case 'run_failed':
      window._cur = null;
      stateEl.textContent = '失败';
      line('运行失败：' + ev.message, 'bad');
      break;
    case 'run_finished':
      window._cur = null;
      break;
  }
}

async function consumeStream(r) {
  if (!threadId) {
    threadId = r.headers.get('X-Thread-Id') || '';
    threadEl.textContent = `会话 ${threadId}`;
  }
  const reader = r.body.getReader();
  const decoder = new TextDecoder();
  let buf = '';
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    const frames = buf.split('\n\n');
    buf = frames.pop();
    for (const frame of frames) {
      const payload = frame.replace(/^data: /, '');
      if (payload) { try { render(JSON.parse(payload)); } catch (e) {} }
    }
  }
  if (pendingApprovals.length) await answerApprovals();
}

async function answerApprovals() {
  const approvals = pendingApprovals;
  pendingApprovals = [];
  const decisions = {};
  for (const ev of approvals) {
    decisions[ev.request_id] = window.confirm(
      `批准 ${ev.tool}？\n${ev.command}\n${ev.reason || ''}`
    );
  }
  const r = await authFetch(`/api/resume/${threadId}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ decisions }),
  });
  if (r.ok) await consumeStream(r);
  else line('恢复失败：' + (await r.text()), 'bad');
}

async function send() {
  const text = promptEl.value.trim();
  if (!text || busy) return;
  promptEl.value = '';
  line('你 › ' + text, 'you');
  window._answered = false;
  window._cur = null;
  pendingApprovals = [];
  setBusy(true);
  stateEl.textContent = '运行中…';

  try {
    const r = await authFetch('/api/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ prompt: text, thread_id: threadId }),
    });
    if (!r.ok) { line('请求失败：' + (await r.text()), 'bad'); return; }
    await consumeStream(r);
  } catch (e) {
    line('请求失败：' + e, 'bad');
  } finally {
    setBusy(false);
    if (!pendingApprovals.length && stateEl.textContent !== '已暂停') {
      stateEl.textContent = '就绪';
    }
    loadSessions();
  }
}

async function steer(action) {
  if (!threadId) return;
  const text = steerTextEl.value.trim();
  try {
    const r = await authFetch(`/api/steer/${threadId}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        action,
        text: action === 'add' || action === 'replan' ? text : '',
        role: steerRoleEl.value,
      }),
    });
    if (r.ok) line(`→ steering ${action} 已提交`, 'tool');
    else line('steering 失败：' + (await r.text()), 'bad');
  } catch (e) {
    line('steering 失败：' + e, 'bad');
  }
}

async function resumeSteering() {
  if (!threadId) return;
  try {
    const r = await authFetch(`/api/resume_steering/${threadId}`, { method: 'POST' });
    if (!r.ok) { line('恢复失败：' + (await r.text()), 'bad'); return; }
    stateEl.textContent = '运行中…';
    await consumeStream(r);
    if (stateEl.textContent !== '已暂停') stateEl.textContent = '就绪';
  } catch (e) {
    line('恢复失败：' + e, 'bad');
  }
}

document.getElementById('pause').addEventListener('click', () => steer('pause'));
document.getElementById('cancel').addEventListener('click', () => steer('cancel'));
document.getElementById('steer').addEventListener('click', () => steer(steerActionEl.value));
document.getElementById('resume').addEventListener('click', resumeSteering);
sendBtn.addEventListener('click', send);
promptEl.addEventListener('keydown', e => { if (e.key === 'Enter') send(); });
document.getElementById('new').addEventListener('click', async () => {
  if (busy) return;
  threadId = '';
  threadEl.textContent = '新会话';
  log.replaceChildren();
  await loadSessions();
  promptEl.focus();
});

document.getElementById('token-save').addEventListener('click', async () => {
  const value = tokenInputEl.value.trim();
  // 空 token 在 loopback 匿名模式下是合法的；远程模式会立即 401 并重新弹窗。
  setToken(value);
  hideLogin();
  await health();
  await loadSessions();
});
tokenInputEl.addEventListener('keydown', e => {
  if (e.key === 'Enter') document.getElementById('token-save').click();
});
logoutBtn.addEventListener('click', () => {
  setToken('');
  sessionStorage.clear();
  showLogin('已退出');
});

health().then(loadSessions);
promptEl.focus();
