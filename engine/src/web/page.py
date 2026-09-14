"""Web 审阅前端页面（单文件 SPA，零构建依赖）。"""

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>小说审阅系统 · __NOVEL_ID__</title>
<style>
  :root { --bg:#0f1115; --panel:#181b22; --border:#2a2f3a; --fg:#e6e8ec;
          --muted:#8a92a3; --accent:#4f8cff; --green:#3fb950; --red:#f85149;
          --yellow:#d29922; }
  * { box-sizing:border-box; }
  body { margin:0; font-family:"Segoe UI",system-ui,"Microsoft YaHei",sans-serif;
         background:var(--bg); color:var(--fg); }
  header { padding:14px 22px; border-bottom:1px solid var(--border);
           display:flex; align-items:center; gap:16px; background:var(--panel); }
  header h1 { font-size:17px; margin:0; font-weight:600; }
  header .id { color:var(--accent); }
  .metrics { margin-left:auto; display:flex; gap:20px; font-size:13px; }
  .metrics b { color:var(--fg); font-size:15px; }
  .metrics span { color:var(--muted); }
  .layout { display:flex; height:calc(100vh - 55px); }
  .sidebar { width:260px; border-right:1px solid var(--border); overflow-y:auto;
             background:var(--panel); }
  .ch { padding:10px 16px; border-bottom:1px solid var(--border); cursor:pointer;
        font-size:13px; display:flex; align-items:center; gap:8px; }
  .ch:hover { background:#20242e; }
  .ch.active { background:#242a36; border-left:3px solid var(--accent); }
  .dot { width:8px; height:8px; border-radius:50%; flex:none; }
  .dot.approved { background:var(--green); }
  .dot.draft { background:var(--yellow); }
  .ch .t { flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .ch .sc { color:var(--muted); font-size:12px; }
  main { flex:1; overflow-y:auto; padding:26px 40px; }
  .content { max-width:820px; line-height:1.9; font-size:15.5px; }
  .content h1,.content h2,.content h3 { line-height:1.4; }
  .review { max-width:820px; margin-top:22px; padding:16px 20px;
            background:var(--panel); border:1px solid var(--border);
            border-radius:8px; font-size:14px; }
  .review table { border-collapse:collapse; }
  .review th,.review td { border:1px solid var(--border); padding:5px 12px; }
  .pending-bar { max-width:820px; margin-bottom:20px; padding:14px 18px;
                 border-radius:8px; background:#1d2530; border:1px solid var(--accent); }
  .pending-bar h3 { margin:0 0 8px; font-size:15px; }
  button { font-family:inherit; font-size:14px; padding:8px 18px; border-radius:6px;
           border:1px solid var(--border); cursor:pointer; margin-right:10px; }
  .btn-approve { background:var(--green); color:#fff; border:none; }
  .btn-reject { background:transparent; color:var(--red); border-color:var(--red); }
  textarea { width:100%; margin-top:10px; background:#0f1115; color:var(--fg);
             border:1px solid var(--border); border-radius:6px; padding:8px;
             font-family:inherit; display:none; }
  .empty { color:var(--muted); margin-top:40px; text-align:center; }
  .badge { font-size:11px; padding:2px 8px; border-radius:10px; background:#242a36;
           color:var(--muted); }
  .badge.fb { background:#3a2a12; color:var(--yellow); border:1px solid var(--yellow); }
  .model-tag { font-size:12px; color:var(--muted); margin-left:6px; }
  .mode-badge { font-size:11px; padding:2px 10px; border-radius:10px;
                background:#1d2530; border:1px solid var(--accent); color:var(--accent); }
  .xvol { max-width:820px; margin-bottom:20px; }
  .xvol-card { padding:12px 18px; margin-bottom:12px; border-radius:8px;
               background:var(--panel); border:1px solid var(--border); }
  .xvol-card.ok { border-left:3px solid var(--green); }
  .xvol-card.bad { border-left:3px solid var(--red); }
  .xvol-card h4 { margin:0 0 6px; font-size:14px; }
  .xvol-card .tag { font-size:11px; padding:1px 8px; border-radius:8px; margin-left:8px; }
  .xvol-card .tag.ok { background:#12331c; color:var(--green); }
  .xvol-card .tag.bad { background:#3a1416; color:var(--red); }
  .xvol-body { font-size:13px; color:var(--muted); margin-top:6px; }
  .xvol-body table { border-collapse:collapse; }
  .xvol-body th,.xvol-body td { border:1px solid var(--border); padding:4px 10px; }
  .queue { max-width:820px; margin-bottom:16px; display:flex; flex-wrap:wrap;
           gap:8px; align-items:center; }
  .queue .q-label { font-size:13px; color:var(--muted); }
  .queue .chip { font-size:12px; padding:4px 12px; border-radius:14px; cursor:pointer;
                 background:var(--panel); border:1px solid var(--yellow);
                 color:var(--yellow); }
  .queue .chip:hover { background:#242a36; }
  .btn-diff { background:transparent; color:var(--accent); border-color:var(--accent); }
  .diff { line-height:1.7; font-size:14px; }
  .diff span { display:block; white-space:pre-wrap; padding:0 6px; border-radius:3px; }
  .diff .d-add { background:#12331c; color:#7ee2a8; }
  .diff .d-del { background:#3a1416; color:#f0938e; text-decoration:line-through; }
  .trend { max-width:820px; margin-bottom:20px; padding:14px 18px;
           background:var(--panel); border:1px solid var(--border); border-radius:8px; }
  .trend h3 { margin:0 0 8px; font-size:14px; }
  .revision-mode { margin:10px 0 6px; font-size:13px; color:var(--muted); }
  .revision-mode label { margin-right:14px; color:var(--fg); cursor:pointer; }
  .quick-chips { display:flex; flex-wrap:wrap; gap:6px; margin-bottom:8px; }
  .qchip { font-size:12px; padding:3px 10px; border-radius:12px; cursor:pointer;
           background:#20242e; border:1px solid var(--border); color:var(--muted); }
  .qchip:hover { border-color:var(--accent); color:var(--accent); }
  .book-bar { display:flex; align-items:center; gap:8px; }
  .book-bar select { background:#0f1115; color:var(--fg); border:1px solid var(--border);
                     border-radius:6px; padding:4px 8px; font-family:inherit; font-size:13px; }
  .btn-shelf { background:transparent; color:var(--accent); border-color:var(--accent);
               padding:4px 12px; font-size:13px; margin:0; }
  .shelf { max-width:820px; margin-bottom:20px; padding:16px 20px;
           background:var(--panel); border:1px solid var(--border); border-radius:8px; }
  .shelf h3 { margin:0 0 12px; font-size:15px; }
  .shelf-grid { display:flex; flex-wrap:wrap; gap:12px; }
  .book-card { width:220px; padding:12px 14px; border-radius:8px; cursor:pointer;
               background:#1d2530; border:1px solid var(--border); }
  .book-card:hover { border-color:var(--accent); }
  .book-card.cur { border-color:var(--accent); border-width:2px; }
  .book-card h4 { margin:0 0 6px; font-size:14px; }
  .book-card .meta { font-size:12px; color:var(--muted); line-height:1.7; }
  .book-card .tag-active { font-size:11px; color:var(--green); }
  .shelf-new { margin-top:14px; display:flex; gap:8px; }
  .shelf-new input { flex:1; max-width:260px; background:#0f1115; color:var(--fg);
                     border:1px solid var(--border); border-radius:6px; padding:6px 10px;
                     font-family:inherit; }
</style>
</head>
<body>
<header>
  <h1>📖 小说审阅系统 · <span class="id" id="curBook">__NOVEL_ID__</span> <span id="mode"></span></h1>
  <div class="book-bar">
    <select id="bookSel" onchange="switchBook(this.value)"></select>
    <button class="btn-shelf" onclick="toggleShelf()">📚 书架</button>
  </div>
  <div class="metrics" id="metrics"></div>
</header>
<div class="layout">
  <div class="sidebar" id="sidebar"></div>
  <main>
    <div id="shelf" style="display:none"></div>
    <div id="pending"></div>
    <div id="queue"></div>
    <div id="trend"></div>
    <div id="crossvol" class="xvol"></div>
    <div id="detail"><div class="empty">← 从左侧选择章节查看，或等待待审项出现</div></div>
  </main>
</div>
<script>
const NOVEL_ID = "__NOVEL_ID__";
let currentNovel = NOVEL_ID;   // 书架当前书（P4-B）：注入所有 fetch
let current = null;

async function api(path, opts) {
  const sep = path.includes("?") ? "&" : "?";
  const r = await fetch("/api" + path + sep + "novel=" + encodeURIComponent(currentNovel), opts);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

function renderMetrics(m) {
  if (!m || !m.total_chapters) { document.getElementById("metrics").innerHTML =
      '<span>暂无数据</span>'; return; }
  const fp = m.first_pass_rate == null ? "—" : m.first_pass_rate + "%";
  const fr = m.foreshadow_rate == null ? "—" :
      m.foreshadow_resolved + "/" + m.foreshadow_total + " (" + m.foreshadow_rate + "%)";
  const av = m.avg_score == null ? "—" : m.avg_score;
  document.getElementById("metrics").innerHTML =
    `<div><b>${m.approved}/${m.total_chapters}</b><br><span>已审/总章</span></div>` +
    `<div><b>${fp}</b><br><span>一次通过率</span></div>` +
    `<div><b>${fr}</b><br><span>伏笔回收</span></div>` +
    `<div><b>${av}</b><br><span>平均分</span></div>`;
}

async function loadChapters() {
  const { chapters } = await api("/chapters");
  const sb = document.getElementById("sidebar");
  if (!chapters.length) { sb.innerHTML = '<div class="empty">尚无章节</div>'; return; }
  sb.innerHTML = chapters.map(c => `
    <div class="ch ${current===c.chapter?'active':''}" onclick="openChapter(${c.chapter})">
      <span class="dot ${c.status}"></span>
      <span class="t">第${c.chapter}章 ${c.title||''}</span>
      <span class="sc">${c.score!=null?c.score:''}</span>
    </div>`).join("");
}

async function openChapter(ch) {
  current = ch;
  await loadChapters();
  const d = await api("/chapters/" + ch);
  let html = `<div class="content"><h2>第${d.chapter}章 ${d.title||''} `
    + `<span class="badge">${d.status}</span></h2>`
    + (d.model ? `<p class="model-tag">🧩 生成模型：${d.model}`
        + (d.used_fallback ? ' <span class="badge fb">⚠ 已降级备用接入点</span>' : '') + `</p>` : '')
    + `${d.content_html}</div>`;
  if (d.review_html) html += `<div class="review"><h3>审查报告</h3>${d.review_html}</div>`;
  document.getElementById("detail").innerHTML = html;
}

let pendingKey = null, lastPending = null;

function renderPending(p) {
  const el = document.getElementById("pending");
  if (!p) { el.innerHTML = ""; pendingKey = null; lastPending = null; return; }
  const key = p.type + ":" + (p.chapter||0) + ":" + (p.attempt||1);
  if (key === pendingKey) return;   // 同一待审项不重绘，避免轮询清空反馈输入
  pendingKey = key; lastPending = p;
  let title, body = "";
  if (p.type === "outline_review") {
    title = "📋 大纲待审：《" + (p.outline.book_title||"") + "》";
    body = "<p>主题：" + (p.outline.theme||"") + "，共 " +
      (p.outline.volumes||[]).reduce((a,v)=>a+(v.chapters||[]).length,0) + " 章</p>";
  } else if (p.type === "chapter_review") {
    title = "✍ 第 " + p.chapter + " 章待审（第 " + (p.attempt||1) + " 稿）";
    if (p.model) body += `<p class="model-tag">🧩 生成模型：${p.model}`
      + (p.used_fallback ? ' <span class="badge fb">⚠ 已降级备用接入点</span>' : '') + `</p>`;
    if (p.retry_exceeded) body += '<p style="color:#f85149">⚠ 自动重试已超限</p>';
    if (p.review) { const r=p.review;
      const ov=((r.consistency+r.plot+r.continuity+r.prose)/4).toFixed(2);
      body += `<p>Editor 评分：<b>${ov}</b>（一致性${r.consistency}/大纲${r.plot}/衔接${r.continuity}/文笔${r.prose}）</p>`;
      if (r.comment) body += `<p><i>${r.comment}</i></p>`;
    }
    if (p.previous_draft) body += `<p><button class="btn-diff" onclick="openDiff()">↔ 对比上一稿（第${p.previous_attempt}稿 → 第${p.attempt||1}稿）</button></p>`;
    if (p.draft_text) openDraft(p.draft_text, p.chapter, p.attempt);
  }
  const isChapter = p.type === "chapter_review";
  const quicks = ["把这一段节奏放慢，增加张力", "删减心理独白，用动作与对话推进",
    "强化结尾钩子", "对话更口语化、更贴合人物", "补充环境与感官细节", "精简冗长描写，叙事更紧凑"];
  const revisionUI = isChapter ?
    `<div class="revision-mode">修订模式：` +
      `<label><input type="radio" name="rmode" value="targeted" checked> 定向修订（仅改指定处）</label>` +
      `<label><input type="radio" name="rmode" value="rewrite"> 整体重写</label></div>` +
      `<div class="quick-chips">` +
      quicks.map(q => `<span class="qchip" onclick="addQuick('${q}')">${q}</span>`).join("") +
      `</div>` : "";
  el.innerHTML = `<div class="pending-bar"><h3>${title}</h3>${body}
    <button class="btn-approve" onclick="decide('approve')">✓ 通过</button>
    <button class="btn-reject" onclick="toggleFeedback()">✗ 打回</button>
    <textarea id="fb" placeholder="请输入打回修改意见，或点击下方常用指令..."></textarea>
    <div id="fb-actions" style="display:none;margin-top:8px">${revisionUI}
      <button class="btn-reject" onclick="decide('reject')">提交打回意见</button>
    </div></div>`;
}

function esc(t) {
  return String(t).replace(/[<>&]/g,c=>({ '<':'&lt;','>':'&gt;','&':'&amp;'}[c]));
}

function openDraft(text, ch, attempt) {
  document.getElementById("detail").innerHTML =
    `<div class="content"><h2>第${ch}章 · 第${attempt}稿（待审）</h2>` +
    `<div style="white-space:pre-wrap">${esc(text)}</div></div>`;
}

// 行级 LCS diff：返回 [op, line] 序列，op ∈ {"=","-","+"}
function lineDiff(a, b) {
  const A = a.split("\n"), B = b.split("\n"), n = A.length, m = B.length;
  const dp = Array.from({length:n+1}, () => new Array(m+1).fill(0));
  for (let i=n-1; i>=0; i--) for (let j=m-1; j>=0; j--)
    dp[i][j] = A[i]===B[j] ? dp[i+1][j+1]+1 : Math.max(dp[i+1][j], dp[i][j+1]);
  const ops = []; let i=0, j=0;
  while (i<n && j<m) {
    if (A[i]===B[j]) { ops.push(["=",A[i]]); i++; j++; }
    else if (dp[i+1][j] >= dp[i][j+1]) { ops.push(["-",A[i]]); i++; }
    else { ops.push(["+",B[j]]); j++; }
  }
  while (i<n) ops.push(["-",A[i++]]);
  while (j<m) ops.push(["+",B[j++]]);
  return ops;
}

function openDiff() {
  const p = lastPending;
  if (!p || !p.previous_draft) return;
  const html = lineDiff(p.previous_draft, p.draft_text||"").map(([op,line]) => {
    const cls = op==="+" ? "d-add" : op==="-" ? "d-del" : "";
    return `<span class="${cls}">${esc(line)||" "}</span>`;
  }).join("");
  document.getElementById("detail").innerHTML =
    `<div class="content"><h2>第${p.chapter}章 · 第${p.previous_attempt}稿 → 第${p.attempt}稿 对比</h2>` +
    `<p><button onclick="openDraft(lastPending.draft_text, lastPending.chapter, lastPending.attempt)">← 返回当前稿</button></p>` +
    `<div class="diff">${html}</div></div>`;
}

async function loadQueue() {
  let q = [];
  try { q = (await api("/queue")).queue || []; } catch(e) { return; }
  const el = document.getElementById("queue");
  if (!q.length) { el.innerHTML = ""; return; }
  el.innerHTML = `<div class="queue"><span class="q-label">⏳ 待审队列（${q.length}）</span>` +
    q.map(it => `<span class="chip" onclick="openChapter(${it.chapter})">` +
      `第${it.chapter}章${it.title ? ' ' + esc(it.title) : ''}` +
      `${it.overall != null ? ' · ' + it.overall + '分' : ''}</span>`).join("") + `</div>`;
}

function renderTrend(hist) {
  const el = document.getElementById("trend");
  if (!hist || !hist.length) { el.innerHTML = ""; return; }
  const W=780, H=120, P=24, n=hist.length;
  const x = i => n===1 ? W/2 : P + i*(W-2*P)/(n-1);
  const y = v => H-P - (v/10)*(H-2*P);
  const pts = hist.map((h,i) => `${x(i)},${y(h.overall)}`).join(" ");
  const dots = hist.map((h,i) =>
    `<circle cx="${x(i)}" cy="${y(h.overall)}" r="3.5" fill="${h.attempt>1?'#d29922':'#4f8cff'}">` +
    `<title>第${h.chapter}章 第${h.attempt}稿：${h.overall}</title></circle>` +
    `<text x="${x(i)}" y="${H-6}" font-size="10" fill="#8a92a3" text-anchor="middle">${h.chapter}-${h.attempt}</text>`
  ).join("");
  el.innerHTML = `<div class="trend"><h3>📈 评分趋势（本次会话，黄点为重稿）</h3>` +
    `<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}">` +
    `<line x1="${P}" y1="${y(6)}" x2="${W-P}" y2="${y(6)}" stroke="#2a2f3a" stroke-dasharray="4 4"/>` +
    `<polyline points="${pts}" fill="none" stroke="#4f8cff" stroke-width="1.5" opacity="0.7"/>` +
    dots + `</svg></div>`;
}

async function loadCrossVolume() {
  let reports = [];
  try { reports = (await api("/cross-volume")).reports || []; } catch(e) { return; }
  const el = document.getElementById("crossvol");
  if (!reports.length) { el.innerHTML = ""; return; }
  el.innerHTML = reports.map(r => {
    const ok = r.consistent !== false;
    const tag = ok ? '<span class="tag ok">一致</span>'
                   : '<span class="tag bad">存在冲突</span>';
    const vols = (r.volumes||[]).join("、");
    return `<div class="xvol-card ${ok?'ok':'bad'}">
      <h4>🔗 跨卷审查 · 波次 ${r.wave}（卷 ${vols}）${tag}
        <span class="badge">${r.issue_count||0} 处问题</span></h4>
      <div class="xvol-body">${r.html||""}</div></div>`;
  }).join("");
}

function toggleFeedback() {
  const fb=document.getElementById("fb"), fa=document.getElementById("fb-actions");
  fb.style.display = fa.style.display = "block";
  fb.focus();
}

function addQuick(t) {
  const fb = document.getElementById("fb");
  fb.value = fb.value.trim() ? fb.value.replace(/\s*$/, "") + "\n" + t : t;
  fb.focus();
}

async function decide(action) {
  const feedback = action==="reject" ? document.getElementById("fb").value : "";
  if (action==="reject" && !feedback.trim()) { alert("请填写打回意见"); return; }
  const rm = document.querySelector('input[name="rmode"]:checked');
  const revision_mode = rm ? rm.value : "targeted";
  await api("/decision", {method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({action, feedback, revision_mode})});
  pendingKey = null;   // 允许下一个待审项（含同键的新大纲稿）重绘
  document.getElementById("pending").innerHTML = "<p style='color:#8a92a3'>已提交，生成中...</p>";
}

// ---------- 书架（P4-B） ----------

let booksCache = [];

async function loadBooks() {
  try { booksCache = (await api("/books")).books || []; } catch(e) { return; }
  const sel = document.getElementById("bookSel");
  sel.innerHTML = booksCache.map(b =>
    `<option value="${b.novel_id}" ${b.novel_id===currentNovel?'selected':''}>` +
    `${esc(b.title)}${b.active?' ●':''}</option>`).join("");
  renderShelf();
}

function renderShelf() {
  const el = document.getElementById("shelf");
  if (el.style.display === "none") return;
  // 轮询重绘保留新书输入框的内容与焦点（避免用户输入被清空）
  const prev = document.getElementById("newBookId");
  const draft = prev ? prev.value : "";
  const hadFocus = prev && document.activeElement === prev;
  const cards = booksCache.map(b => {
    const rate = b.chapters ? Math.round(b.approved / b.chapters * 100) : 0;
    return `<div class="book-card ${b.novel_id===currentNovel?'cur':''}"
      onclick="switchBook('${b.novel_id}')">
      <h4>${esc(b.title)} ${b.active?'<span class="tag-active">● 生成中</span>':''}</h4>
      <div class="meta">ID：${b.novel_id}<br>进度：${b.approved}/${b.chapters} 章（${rate}%）</div>
    </div>`;
  }).join("");
  el.innerHTML = `<div class="shelf"><h3>📚 书架</h3>
    <div class="shelf-grid">${cards || '<span class="q-label">暂无书目</span>'}</div>
    <div class="shelf-new"><input id="newBookId" placeholder="新书 ID（字母/数字/下划线/连字符）"/>
      <button class="btn-shelf" onclick="createBook()">＋ 新建书</button></div></div>`;
  const inp = document.getElementById("newBookId");
  if (inp) { inp.value = draft; if (hadFocus) inp.focus(); }
}

function toggleShelf() {
  const el = document.getElementById("shelf");
  el.style.display = el.style.display === "none" ? "block" : "none";
  renderShelf();
}

async function createBook() {
  const nid = document.getElementById("newBookId").value.trim();
  if (!nid) { alert("请输入新书 ID"); return; }
  try {
    await api("/books", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({novel_id: nid})});
  } catch(e) { alert("新建失败：" + e.message); return; }
  await loadBooks();
  switchBook(nid);
}

async function switchBook(nid) {
  if (nid === currentNovel) return;
  currentNovel = nid;
  // 重置会话级轮询状态与待审缓存（不同书互不串台）
  current = null; pendingKey = null; lastPending = null;
  document.getElementById("curBook").textContent = nid;
  document.getElementById("detail").innerHTML =
    '<div class="empty">← 从左侧选择章节查看，或等待待审项出现</div>';
  document.getElementById("pending").innerHTML = "";
  document.getElementById("trend").innerHTML = "";
  document.getElementById("crossvol").innerHTML = "";
  document.getElementById("queue").innerHTML = "";
  await loadBooks();
  await loadChapters();
  await poll();
}

async function poll() {
  try {
    const s = await api("/status");
    renderMetrics(s.metrics);
    renderPending(s.pending);
    renderTrend(s.review_history || []);
    document.getElementById("mode").innerHTML =
        s.parallel ? '<span class="mode-badge">⚡ 并行模式</span>' : '';
    await loadCrossVolume();
    await loadQueue();
    if (!current) await loadChapters();
    if (s.error) document.getElementById("pending").innerHTML =
        `<div class="pending-bar" style="border-color:#f85149"><b>生成异常：</b>${s.error}</div>`;
  } catch(e) { /* ignore transient */ }
}
poll();
loadBooks();
setInterval(poll, 2500);
setInterval(loadChapters, 5000);
setInterval(loadBooks, 10000);
</script>
</body>
</html>
"""
