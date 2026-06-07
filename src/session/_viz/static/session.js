/* ================================================================
   Session Inspector — chat-bubble rendering + white-box editing
   ================================================================ */

let pollTimer = null, pollActive = true;
const POLL_MS = 2000;
let prevTurnCount = 0;  // track new content for smart scroll
let lastDataHash = '';  // prevent re-render when nothing changed

// ---- Dark mode ----
function initDark() {
  const saved = localStorage.getItem('session-viz-theme');
  if (saved === 'dark') {
    document.documentElement.setAttribute('data-theme', 'dark');
    document.getElementById('dark-btn').textContent = '☀️';
  }
}
function toggleDark() {
  const isDark = document.documentElement.getAttribute('data-theme') === 'dark';
  const next = isDark ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', next);
  localStorage.setItem('session-viz-theme', next);
  document.getElementById('dark-btn').textContent = next === 'dark' ? '☀️' : '🌙';
}
initDark();

// ---- API ----
async function fetchSession() {
  const r = await fetch('/api/session');
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}
async function loadDemo() {
  await fetch('/api/session/demo', { method: 'POST' });
  prevTurnCount = 0; lastDataHash = '';
  refresh();
}
async function clearSession() {
  if (!confirm('确定要清空所有对话记录吗？')) return;
  await fetch('/api/session/clear', { method: 'POST' });
  prevTurnCount = 0; lastDataHash = '';
  refresh();
}

async function editMessage(msgId, newContent) {
  await fetch(`/api/session/message/${msgId}/edit`, {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({content: newContent})
  });
  refresh();
}
async function deleteMessage(msgId) {
  if (!confirm('确定删除这条消息吗？（关联的工具调用也会被移除）')) return;
  await fetch(`/api/session/message/${msgId}/delete`, { method: 'POST' });
  refresh();
}
async function retryTool(toolUseId) {
  await fetch(`/api/session/tool/${toolUseId}/retry`, { method: 'POST' });
  refresh();
}
async function rollbackTo(msgId) {
  if (!confirm('确定回退到此消息之前吗？此消息及之后的所有内容将被删除。')) return;
  await fetch(`/api/session/rollback/${msgId}`, { method: 'POST' });
  prevTurnCount = 0; lastDataHash = '';
  refresh();
}
async function injectCorrection(text) {
  const t = text || prompt('请输入修正提示，引导 AI 回到正确的方向：');
  if (!t) return;
  await fetch('/api/session/inject', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({text: t})
  });
  refresh();
}

// ---- Helpers ----
function esc(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}
function fmtMs(ms) {
  if (ms == null) return '';
  if (ms < 1000) return `${ms}ms`;
  return `${(ms/1000).toFixed(1)}s`;
}
function fmtTime(ts) {
  if (!ts) return '';
  return new Date(ts).toLocaleTimeString('zh-CN', {hour:'2-digit',minute:'2-digit',second:'2-digit'});
}

// ---- Tool display helpers ----
const TOOL_META = {
  Read:  { icon: '📖', cls: 'read',  verb: '读取文件' },
  Write: { icon: '✏️', cls: 'write', verb: '写入文件' },
  Edit:  { icon: '📝', cls: 'write', verb: '编辑文件' },
  Bash:  { icon: '⚡', cls: 'exec',  verb: '执行命令' },
  Glob:  { icon: '🔍', cls: 'search',verb: '搜索文件' },
  Grep:  { icon: '🔎', cls: 'search',verb: '搜索内容' },
  Agent: { icon: '🤖', cls: 'agent', verb: '启动智能体' },
};

function toolDisplay(tc) {
  const meta = TOOL_META[tc.name] || { icon: '🔧', cls: 'exec', verb: tc.name };
  const input = tc.input || {};
  let detail = '';
  if (tc.name === 'Read' || tc.name === 'Edit' || tc.name === 'Write') {
    detail = input.file_path || '';
  } else if (tc.name === 'Bash') {
    detail = (input.command || '').substring(0, 80);
  } else if (tc.name === 'Glob' || tc.name === 'Grep') {
    detail = input.pattern || '';
    if (input.path) detail += ' 在 ' + input.path;
  } else if (tc.name === 'Agent') {
    detail = input.description || '';
  } else {
    detail = JSON.stringify(input).substring(0, 60);
  }
  return { ...meta, detail };
}

function statusBadge(tc) {
  const s = tc.status || 'pending';
  const map = {
    pending:   ['pending',  '⏳ 等待中'],
    executing: ['running',  '⏳ 执行中…'],
    completed: ['done',     '✓ 完成'],
    errored:   ['error',    '✗ 失败'],
  };
  const [cls, label] = map[s] || ['pending', s];
  const elapsed = tc.elapsed_ms ? ` · ${fmtMs(tc.elapsed_ms)}` : '';
  return `<span class="tool-status-badge ${cls}">${label}${elapsed}</span>`;
}

function renderToolCard(tc) {
  const d = toolDisplay(tc);
  const badge = statusBadge(tc);
  const hasResult = tc.result && tc.result.content;
  const isErr = tc.result && tc.result.is_error;

  return `
    <div class="tool-card" data-tool-id="${esc(tc.tool_use_id)}">
      <div class="tool-card-header" onclick="this.parentElement.classList.toggle('open')">
        <div class="tool-icon ${d.cls}">${d.icon}</div>
        <div class="tool-info">
          <div class="tool-action">${esc(d.verb)}</div>
          <div class="tool-detail">${esc(d.detail)}</div>
        </div>
        ${badge}
        <span class="tool-chevron">▾</span>
      </div>
      <div class="tool-card-body">
        <div class="tool-section">
          <div class="tool-section-label">📥 输入参数</div>
          <div class="tool-section-content">${esc(formatToolInput(tc.name, tc.input))}</div>
        </div>
        ${hasResult ? `
        <div class="tool-section">
          <div class="tool-section-label">${isErr ? '⚠️ 错误输出' : '📤 执行结果'}</div>
          <div class="tool-section-content${isErr ? ' error' : ''}">${esc(tc.result.content)}</div>
        </div>
        ` : ''}
        <div style="margin-top:10px;font-size:11px;color:var(--text-dim)">
          ID: ${esc(tc.tool_use_id)} ·
          开始: ${fmtTime(tc.started_at) || '—'} ·
          结束: ${fmtTime(tc.completed_at) || '—'}
          ${tc.elapsed_ms != null ? ' · 耗时: ' + fmtMs(tc.elapsed_ms) : ''}
        </div>
      </div>
      <div class="tool-card-actions">
        <button onclick="event.stopPropagation();retryTool('${esc(tc.tool_use_id)}')" title="重置为待执行状态">🔄 重试</button>
        <button class="danger" onclick="event.stopPropagation();deleteMessage('${esc(tc.id)}')" title="删除此工具调用">🗑 删除</button>
      </div>
    </div>`;
}

function formatToolInput(name, input) {
  if (!input || Object.keys(input).length === 0) return '(无参数)';
  const lines = [];
  for (const [k, v] of Object.entries(input)) {
    const val = typeof v === 'string' ? v : JSON.stringify(v);
    lines.push(`${k}: ${val}`);
  }
  return lines.join('\n');
}

// ---- Markdown renderer ----
function renderMarkdown(text) {
  let out = esc(text);
  out = out.replace(/```(\w*)\n([\s\S]*?)```/g, (_, lang, code) =>
    `<pre><code>${code.trim()}</code></pre>`);
  out = out.replace(/`([^`]+)`/g, '<code>$1</code>');
  out = out.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  out = out.replace(/\*([^*]+)\*/g, '<em>$1</em>');
  out = out.replace(/\n/g, '<br>');
  return out;
}

// ---- Inline editing ----
function startEdit(msgId, currentText) {
  const bubble = document.querySelector(`[data-msg-id="${msgId}"]`);
  if (!bubble) return;
  const unescaped = currentText.replace(/<br>/g, '\n');
  bubble.innerHTML = `
    <textarea class="edit-area">${esc(unescaped)}</textarea>
    <div class="edit-actions">
      <button class="btn-cancel" onclick="event.stopPropagation();refresh()">取消</button>
      <button class="btn-save" onclick="event.stopPropagation();saveEdit('${msgId}', this.parentElement.parentElement.querySelector('textarea').value)">保存</button>
    </div>`;
}
function saveEdit(msgId, newText) {
  editMessage(msgId, newText);
}

// ---- Stats ----
function renderStats(stats) {
  document.getElementById('stat-turns').textContent = stats.total_turns;
  document.getElementById('stat-tools').textContent = stats.total_tool_calls;
  document.getElementById('stat-ok').textContent = stats.tool_calls_completed;
  document.getElementById('stat-err').textContent = stats.tool_calls_errored;
  const total = (stats.total_input_tokens||0) + (stats.total_output_tokens||0);
  document.getElementById('stat-tokens').textContent = total.toLocaleString();
}

// ---- Main render ----
function renderChat(data) {
  document.getElementById('sid').textContent = '会话 ' + (data.session_id||'—').substring(0,8);
  renderStats(data.stats || {});

  const chat = document.getElementById('chat');
  const turns = data.turns || [];
  const isNewContent = turns.length > prevTurnCount;
  prevTurnCount = turns.length;

  if (turns.length === 0) {
    chat.innerHTML = `
      <div class="empty-state">
        <div class="empty-illustration">💬</div>
        <h2>还没有对话记录</h2>
        <p>点击 <strong>「📥 加载示例」</strong> 查看演示数据，<br>或通过 API 接口推送真实会话数据。</p>
        <button class="btn btn-primary" onclick="loadDemo()" style="font-size:15px;padding:10px 24px">
          📥 加载示例数据
        </button>
      </div>`;
    return;
  }

  let html = '';
  for (let i = 0; i < turns.length; i++) {
    const turn = turns[i];
    const tStart = fmtTime(turn.start_time);
    const tElapsed = turn.elapsed_ms ? ` · ${fmtMs(turn.elapsed_ms)}` : '';
    const tcCount = turn.assistant_messages.reduce(
      (s, am) => s + (am.blocks||[]).filter(b => b.type==='tool_call').length, 0);

    html += `<div class="turn-divider">
      <span>第 ${i+1} 轮 · ${tStart}${tElapsed} · ${tcCount} 次工具调用</span>
      <span class="rollback-link" onclick="rollbackTo('${esc(turn.user_message.id)}')" title="回退到此轮之前">↩ 回退到此</span>
    </div>`;

    // User message — right aligned
    const isCorrection = turn.user_message.meta && turn.user_message.meta.correction;
    const corrBadge = isCorrection ? '<span class="correction-badge">修正</span>' : '';
    html += `<div class="chat-msg user">
      <div class="bubble" data-msg-id="${esc(turn.user_message.id)}">
        ${renderMarkdown(turn.user_message.text)}
        <div class="msg-actions">
          <button onclick="event.stopPropagation();startEdit('${esc(turn.user_message.id)}','${esc(turn.user_message.text).replace(/'/g,"\\'")}')">✏️ 编辑</button>
          <button onclick="event.stopPropagation();deleteMessage('${esc(turn.user_message.id)}')">🗑</button>
        </div>
      </div>
      <div class="avatar">👤${corrBadge}</div>
    </div>`;

    // Assistant messages
    for (const am of turn.assistant_messages) {
      const textBlocks = (am.blocks||[]).filter(b => b.type==='text');
      const toolBlocks = (am.blocks||[]).filter(b => b.type==='tool_call');

      if (textBlocks.length > 0) {
        const text = textBlocks.map(b => b.text).join('');
        html += `<div class="chat-msg assistant">
          <div class="avatar">🤖</div>
          <div class="bubble" data-msg-id="${esc(am.id)}">
            ${renderMarkdown(text)}
            <div class="msg-actions">
              <button onclick="event.stopPropagation();startEdit('${esc(am.id)}','${esc(text).replace(/'/g,"\\'").replace(/\n/g,'<br>')}')">✏️ 编辑</button>
              <button onclick="event.stopPropagation();deleteMessage('${esc(am.id)}')">🗑</button>
            </div>
          </div>
        </div>`;
      }

      if (toolBlocks.length > 0) {
        html += toolBlocks.map(renderToolCard).join('');
      }
    }
  }

  // Inject correction button at bottom
  html += `<div style="text-align:center;margin-top:24px;padding-bottom:40px">
    <button class="btn" onclick="injectCorrection()" style="font-size:13px">
      💉 注入修正提示
    </button>
  </div>`;

  chat.innerHTML = html;

  // Smart scroll: only auto-scroll when new turns arrived, and user is near bottom
  if (isNewContent || prevTurnCount === turns.length) {
    const distFromBottom = document.documentElement.scrollHeight - window.innerHeight - window.scrollY;
    if (distFromBottom < 300) {
      requestAnimationFrame(() => {
        window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' });
      });
    }
  }
}

// ---- Polling ----
async function refresh() {
  try {
    const data = await fetchSession();
    // Only re-render if data actually changed (prevents flicker)
    const hash = JSON.stringify(data);
    if (hash === lastDataHash) return;  // nothing changed, skip
    lastDataHash = hash;
    renderChat(data);
  } catch (err) {
    console.error('获取会话数据失败:', err);
  }
}
function togglePoll() {
  pollActive = !pollActive;
  pollActive ? startPoll() : stopPoll();
}
function startPoll() {
  if (pollTimer) return;
  pollTimer = setInterval(() => { refresh(); loadCheckpoints(); }, POLL_MS);
}
function stopPoll() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
}

// ---- Checkpoints ----
async function createCheckpoint() {
  const label = prompt('检查点名称（描述当前状态）：', '');
  if (!label) return;
  const r = await fetch('/api/session/checkpoint', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({label})
  });
  const d = await r.json();
  if (d.ok) { lastDataHash = ''; refresh(); }
}
async function restoreCheckpoint(cpId) {
  if (!confirm('确定恢复到这个检查点吗？之后的对话将被移除。')) return;
  await fetch(`/api/session/checkpoint/${cpId}/restore`, { method: 'POST' });
  lastDataHash = ''; prevTurnCount = 0; refresh();
}
async function branchCheckpoint(cpId) {
  const name = prompt('新分支名称：', 'branch');
  if (!name) return;
  const r = await fetch(`/api/session/checkpoint/${cpId}/branch`, {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({name})
  });
  const d = await r.json();
  if (d.ok) { lastDataHash = ''; refresh(); }
  else alert('分支创建失败: ' + (d.error || 'unknown'));
}
async function deleteCheckpoint(cpId) {
  if (!confirm('删除此检查点？')) return;
  await fetch(`/api/session/checkpoint/${cpId}`, { method: 'DELETE' });
  lastDataHash = ''; refresh();
}
async function switchBranch(name) {
  if (!confirm(`切换到分支 "${name}"？当前未保存的对话将丢失。`)) return;
  const r = await fetch(`/api/session/branch/${name}`, { method: 'POST' });
  const d = await r.json();
  if (d.ok) { lastDataHash = ''; prevTurnCount = 0; refresh(); }
  else alert('切换失败: ' + (d.error || 'unknown'));
}
function showCpPopover(cpId, evt) {
  // Close any open popover
  document.querySelectorAll('.cp-popover.show').forEach(p => p.classList.remove('show'));
  const popover = document.getElementById('pop-' + cpId);
  if (popover) {
    popover.classList.toggle('show');
    evt.stopPropagation();
  }
}
// Close popovers on outside click
document.addEventListener('click', () => {
  document.querySelectorAll('.cp-popover.show').forEach(p => p.classList.remove('show'));
});

async function loadCheckpoints() {
  try {
    const r = await fetch('/api/session/checkpoints');
    const d = await r.json();
    const cps = d.checkpoints || [];
    const branches = d.branches || [];
    const bar = document.getElementById('cp-bar');
    const list = document.getElementById('cp-list');
    const branchTag = document.getElementById('branch-tag');

    // Branch indicator
    const currentBranch = branches.length > 0 ? branches[0] : 'main';
    if (cps.length > 0) {
      branchTag.textContent = '🌿 ' + currentBranch;
      branchTag.style.display = 'inline';
    } else {
      branchTag.style.display = 'none';
    }

    if (cps.length === 0) {
      bar.style.display = 'none';
      return;
    }
    bar.style.display = '';

    list.innerHTML = cps.map(cp => {
      const gitInfo = cp.git_sha
        ? `<span class="cp-git-info">${cp.git_dirty ? '📝' : '✅'} ${cp.git_sha}</span>`
        : '';
      return `
        <span class="cp-anchor">
          <span class="cp-dot" onclick="showCpPopover('${cp.id}',event)" title="${esc(cp.label)} · ${cp.branch}">
            <span class="cp-dot-marker${cp.git_dirty ? ' dirty' : ''}"></span>
            ${esc(cp.label.substring(0,20))}
            ${gitInfo}
          </span>
          <span class="cp-popover" id="pop-${cp.id}">
            <div style="padding:6px 10px;font-size:11px;color:var(--text-dim);border-bottom:1px solid var(--border);margin-bottom:4px">
              <strong>${esc(cp.label)}</strong><br>
              🌿 ${esc(cp.branch)} ·
              ${cp.git_sha ? '🔖 ' + cp.git_sha : '无 git 信息'}<br>
              ${cp.git_dirty ? '📝 有未提交更改' : '✅ 工作区干净'}<br>
              💬 ${cp.message_count} 条消息
            </div>
            <button onclick="event.stopPropagation();restoreCheckpoint('${cp.id}')">↩ 恢复到此</button>
            <button onclick="event.stopPropagation();branchCheckpoint('${cp.id}')">🌿 创建分支</button>
            <button class="danger" onclick="event.stopPropagation();deleteCheckpoint('${cp.id}')">🗑 删除</button>
          </span>
        </span>`;
    }).join(' · ');

    // Branch switcher
    if (branches.length > 1) {
      list.innerHTML += branches.filter(b => b !== currentBranch).map(b =>
        `<span class="cp-dot" onclick="switchBranch('${esc(b)}')" style="border-style:dashed" title="切换到分支 ${esc(b)}">🌿 ${esc(b)}</span>`
      ).join('');
    }
  } catch (err) {
    console.error('加载检查点失败:', err);
  }
}

// ---- Init ----
refresh();
loadCheckpoints();
startPoll();
