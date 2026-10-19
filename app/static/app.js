/* 本地批量下载器 - 前端逻辑 */
"use strict";

const state = {
  tasks: new Map(),     // id -> task
  summary: {},
  filter: "all",
  search: "",
  page: 1,
  pageSize: 100,
  parseResults: [],
  resources: [],        // 扁平化资源列表
  resFilter: "all",     // 当前资源分类
  resSort: "name",      // 资源排序
  resSearch: "",        // 资源搜索
  config: {},
  selected: new Set(),  // 勾选中的任务 id
};

const $ = (id) => document.getElementById(id);

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(data.detail || data.error || `请求失败 (${res.status})`);
  }
  return data;
}

/* ---------- 工具 ---------- */
function fmtBytes(n) {
  if (!n || n <= 0) return "0 B";
  const u = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.min(u.length - 1, Math.floor(Math.log(n) / Math.log(1024)));
  return (n / Math.pow(1024, i)).toFixed(i === 0 ? 0 : 1) + " " + u[i];
}
function fmtSpeed(n) {
  if (!n || n <= 0) return "0 B/s";
  return fmtBytes(n) + "/s";
}
function fmtEta(sec) {
  if (!sec || sec <= 0 || sec === Infinity) return "";
  if (sec < 60) return Math.round(sec) + " 秒";
  if (sec < 3600) return Math.round(sec / 60) + " 分";
  return (sec / 3600).toFixed(1) + " 小时";
}
function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* ---------- 配置 ---------- */
async function loadConfig() {
  state.config = await api("/api/config");
  $("cfg-dir").value = state.config.download_dir || "";
  $("cfg-global").value = state.config.global_concurrency;
  $("cfg-domain").value = state.config.per_domain_concurrency;
  $("cfg-timeout").value = state.config.timeout;
  $("cfg-retries").value = state.config.max_retries;
  $("cfg-conflict").value = state.config.conflict_policy;
  $("cfg-classify").value = state.config.classify_by;
  $("cfg-dedupe").checked = !!state.config.dedupe;
  $("cfg-hs-global").value = state.config.high_speed_global_concurrency ?? 8;
  $("cfg-hs-domain").value = state.config.high_speed_per_domain_concurrency ?? 4;
  $("cfg-chunk-threshold").value = Math.round((state.config.chunk_threshold || 0) / 1024 / 1024) || 10;
  $("cfg-max-chunks").value = state.config.max_chunks_per_file ?? 4;
  $("highspeed-switch").checked = !!state.config.high_speed;
  $("dir-input").placeholder = "默认：" + state.config.resolved_download_dir;
}

async function saveConfig() {
  const body = {
    download_dir: $("cfg-dir").value.trim(),
    global_concurrency: parseInt($("cfg-global").value, 10),
    per_domain_concurrency: parseInt($("cfg-domain").value, 10),
    timeout: parseFloat($("cfg-timeout").value),
    max_retries: parseInt($("cfg-retries").value, 10),
    conflict_policy: $("cfg-conflict").value,
    classify_by: $("cfg-classify").value,
    dedupe: $("cfg-dedupe").checked,
    high_speed: $("highspeed-switch").checked,
    high_speed_global_concurrency: parseInt($("cfg-hs-global").value, 10),
    high_speed_per_domain_concurrency: parseInt($("cfg-hs-domain").value, 10),
    chunk_threshold: Math.round(parseFloat($("cfg-chunk-threshold").value) * 1024 * 1024),
    max_chunks_per_file: parseInt($("cfg-max-chunks").value, 10),
  };
  try {
    state.config = await api("/api/config", { method: "POST", body });
    $("dir-input").placeholder = "默认：" + state.config.resolved_download_dir;
    toast("设置已保存");
  } catch (e) { alert(e.message); }
}

/* ---------- 任务列表 ---------- */
async function loadTasks() {
  const data = await api("/api/tasks");
  state.tasks = new Map(data.tasks.map((t) => [t.id, t]));
  state.summary = data.summary;
  render();
}

function filteredTasks() {
  const arr = [...state.tasks.values()];
  const q = state.search.trim().toLowerCase();
  return arr.filter((t) => {
    if (state.filter !== "all" && t.status !== state.filter) return false;
    if (q && !(t.filename || "").toLowerCase().includes(q) && !(t.url || "").toLowerCase().includes(q)) return false;
    return true;
  }).sort((a, b) => a.id - b.id);
}

function render() {
  renderStats();
  renderTable();
}

function renderStats() {
  const s = state.summary;
  const set = (k, v) => { const el = document.querySelector(`[data-stat="${k}"] b`); if (el) el.textContent = v; };
  set("total", s.total || 0);
  set("pending", s.pending || 0);
  set("downloading", s.downloading || 0);
  set("completed", s.completed || 0);
  set("failed", s.failed || 0);
  set("paused", s.paused || 0);
  set("skipped", s.skipped || 0);
  let speed = 0;
  for (const t of state.tasks.values()) if (t.status === "downloading") speed += t.speed || 0;
  set("speed", fmtSpeed(speed));
  set("conn", s.active_connections || 0);
  const hs = $("highspeed-switch");
  if (hs) hs.checked = !!state.config.high_speed;
}

function renderTable() {
  const arr = filteredTasks();
  const totalPages = Math.max(1, Math.ceil(arr.length / state.pageSize));
  if (state.page > totalPages) state.page = totalPages;
  const start = (state.page - 1) * state.pageSize;
  const pageArr = arr.slice(start, start + state.pageSize);

  const body = $("task-body");
  body.innerHTML = "";
  $("empty-hint").classList.toggle("hidden", arr.length > 0);

  for (const t of pageArr) {
    body.insertAdjacentHTML("beforeend", rowHTML(t));
  }
  // 恢复勾选状态
  body.querySelectorAll(".row-check").forEach((c) => {
    c.checked = state.selected.has(parseInt(c.dataset.id, 10));
  });
  const allChecked = pageArr.length > 0 && pageArr.every((t) => state.selected.has(t.id));
  $("check-all").checked = allChecked;
  $("page-info").textContent = `第 ${state.page}/${totalPages} 页 · 共 ${arr.length} 条`;
  $("prev-page").disabled = state.page <= 1;
  $("next-page").disabled = state.page >= totalPages;
}

function rowHTML(t) {
  const pct = t.progress || 0;
  const done = t.status === "completed";
  const err = t.status === "failed";
  const canPause = t.status === "downloading" || t.status === "pending";
  const canResume = t.status === "paused" || t.status === "failed";
  const canCancel = ["pending", "downloading", "paused"].includes(t.status);
  const canRetry = t.status === "failed";
  const canOpen = t.status === "completed" && t.final_path;
  const sizeText = done && t.total_bytes > 0
    ? fmtBytes(t.total_bytes)
    : `${fmtBytes(t.downloaded_bytes)}${t.total_bytes > 0 ? " / " + fmtBytes(t.total_bytes) : ""}`;

  const errLine = (t.error && (err || t.status === "paused"))
    ? `<div class="err-msg">${esc(t.error)}</div>` : "";

  const speedEta = t.status === "downloading"
    ? `<div class="speed-line">${fmtSpeed(t.speed)}${t.eta > 0 ? " · 剩余 " + fmtEta(t.eta) : ""}</div>`
    : "";

  return `
    <tr data-id="${t.id}">
      <td class="col-check"><input type="checkbox" class="row-check" data-id="${t.id}"></td>
      <td class="col-name">
        <div class="task-name">
          <div class="fname"><span class="badge">${esc(t.filetype || "file")}</span>${esc(t.filename || t.url)}</div>
          <div class="furl" title="${esc(t.url)}">${esc(t.url)}</div>
        </div>
      </td>
      <td class="col-size"><div class="size-cell">${sizeText}</div></td>
      <td class="col-progress">
        <div class="progress-wrap">
          <div class="progress-bar"><div class="fill ${done ? "done" : err ? "err" : ""}" style="width:${pct}%"></div></div>
          <span class="pct">${pct}%</span>
        </div>
        ${speedEta}
      </td>
      <td class="col-status">
        <span class="status-pill ${t.status}">${esc(t.status_label || t.status)}</span>
        ${errLine}
      </td>
      <td class="col-actions"><div class="actions">
        ${canPause ? `<button class="mini" data-act="pause" data-id="${t.id}">暂停</button>` : ""}
        ${canResume ? `<button class="mini" data-act="resume" data-id="${t.id}">继续</button>` : ""}
        ${canCancel ? `<button class="mini" data-act="cancel" data-id="${t.id}">取消</button>` : ""}
        ${canRetry ? `<button class="mini" data-act="retry" data-id="${t.id}">重试</button>` : ""}
        ${canOpen ? `<button class="mini" data-act="open" data-id="${t.id}">打开</button>` : ""}
        ${canOpen ? `<button class="mini" data-act="open-dir" data-id="${t.id}">目录</button>` : ""}
      </div></td>
    </tr>`;
}

/* ---------- 操作 ---------- */
async function act(route, ids) {
  try {
    await api(route, { method: "POST", body: { ids } });
  } catch (e) { alert(e.message); }
}

function selectedIds() {
  return [...document.querySelectorAll(".row-check:checked")].map((c) => parseInt(c.dataset.id, 10));
}

async function addTasks() {
  const text = $("url-input").value.trim();
  const dir = $("dir-input").value.trim();
  if (!text) { $("add-feedback").className = "feedback err"; $("add-feedback").textContent = "请先输入下载链接"; return; }
  const fb = $("add-feedback");
  fb.className = "feedback"; fb.textContent = "正在添加…";
  try {
    const r = await api("/api/tasks", { method: "POST", body: { text, save_dir: dir || undefined } });
    fb.className = "feedback ok";
    fb.textContent = `已添加 ${r.added} 个任务${r.skipped ? `，去重跳过 ${r.skipped} 个` : ""}`;
    $("url-input").value = "";
    await loadTasks();
  } catch (e) {
    fb.className = "feedback err"; fb.textContent = e.message;
  }
}

async function importFile(file) {
  const fb = $("add-feedback");
  try {
    const text = await file.text();
    const r = await api("/api/tasks", { method: "POST", body: { text, save_dir: $("dir-input").value.trim() || undefined } });
    fb.className = "feedback ok";
    fb.textContent = `已从 ${file.name} 导入 ${r.added} 个任务${r.skipped ? `，去重跳过 ${r.skipped} 个` : ""}`;
    await loadTasks();
  } catch (e) {
    fb.className = "feedback err"; fb.textContent = e.message;
  }
}

/* ---------- 网页解析 ---------- */
async function parsePages() {
  const text = $("url-input").value.trim();
  const urls = text.split(/\r?\n/).map((s) => s.trim()).filter((s) => /^https?:\/\//i.test(s));
  if (!urls.length) { alert("请输入网页地址（每行一个）"); return; }
  const fb = $("add-feedback");
  fb.className = "feedback"; fb.textContent = "正在解析网页并识别资源…";
  try {
    const r = await api("/api/parse", { method: "POST", body: { urls } });
    state.parseResults = r.results;
    state.resources = [];
    for (const g of r.results) {
      for (const res of (g.resources || [])) state.resources.push(res);
    }
    state.resFilter = "all";
    state.resSort = "name";
    state.resSearch = "";
    $("res-search").value = "";
    $("res-sort").value = "name";
    fb.textContent = "";
    renderResourceCats();
    renderResources();
    $("parse-modal").classList.remove("hidden");
  } catch (e) {
    fb.className = "feedback err"; fb.textContent = e.message;
  }
}

const CAT_ORDER = ["文档", "表格", "演示文稿", "压缩包", "图片", "视频", "音频", "程序与安装包", "数据与其他文件", "未知类型"];

function resourceCategoryCounts() {
  const counts = { all: state.resources.length };
  for (const r of state.resources) counts[r.category] = (counts[r.category] || 0) + 1;
  return counts;
}

function renderResourceCats() {
  const counts = resourceCategoryCounts();
  const el = $("resource-cats");
  let html = `<button class="res-cat${state.resFilter === "all" ? " active" : ""}" data-cat="all">全部资源（${counts.all}）</button>`;
  for (const c of CAT_ORDER) {
    if (!counts[c]) continue;
    html += `<button class="res-cat${state.resFilter === c ? " active" : ""}" data-cat="${esc(c)}">${esc(c)}（${counts[c]}）</button>`;
  }
  el.innerHTML = html;
}

function resSelectable(r) {
  return r.status === "available" || r.status === "unknown_type";
}

function filteredResources() {
  let arr = state.resources.slice();
  if (state.resFilter !== "all") arr = arr.filter((r) => r.category === state.resFilter);
  const q = state.resSearch.trim().toLowerCase();
  if (q) {
    arr = arr.filter((r) =>
      (r.filename || "").toLowerCase().includes(q) ||
      (r.filetype || "").toLowerCase().includes(q) ||
      (r.source_domain || "").toLowerCase().includes(q) ||
      (r.url || "").toLowerCase().includes(q));
  }
  const sort = state.resSort;
  arr.sort((a, b) => {
    if (sort === "size") return (b.size || 0) - (a.size || 0);
    if (sort === "type") return ((a.filetype || "").localeCompare(b.filetype || "")) || ((a.filename || "").localeCompare(b.filename || ""));
    if (sort === "source") return ((a.source_domain || "").localeCompare(b.source_domain || "")) || ((a.filename || "").localeCompare(b.filename || ""));
    return (a.filename || "").localeCompare(b.filename || "");
  });
  return arr;
}

function renderResources() {
  const body = $("parse-body");
  body.innerHTML = "";
  const arr = filteredResources();
  if (!arr.length) {
    body.innerHTML = `<div class="empty">没有匹配的资源。</div>`;
    $("res-summary").textContent = "";
    return;
  }
  for (const r of arr) {
    const sizeText = r.size > 0 ? fmtBytes(r.size) : "未知";
    const sel = resSelectable(r);
    body.insertAdjacentHTML("beforeend", `
      <label class="res-item${sel ? "" : " res-unavail"}">
        <input type="checkbox" class="res-check" data-url="${esc(r.url)}" ${sel ? "" : "disabled"}>
        <span class="res-name">${esc(r.filename || r.url)}</span>
        <span class="res-type">${esc(r.filetype || "?")}</span>
        <span class="res-size">${esc(sizeText)}</span>
        <span class="res-domain">${esc(r.source_domain || "?")}</span>
        <span class="res-status ${esc(r.status)}">${esc(r.status_label || r.status)}</span>
        <span class="res-url" title="${esc(r.url)}">${esc(r.url)}</span>
        ${r.note ? `<span class="res-note">${esc(r.note)}</span>` : ""}
      </label>`);
  }
  $("res-summary").textContent = `当前显示 ${arr.length} 项`;
}

async function addParsed() {
  const urls = [...document.querySelectorAll(".res-check:checked")].map((c) => c.dataset.url);
  if (!urls.length) { alert("请选择要下载的资源"); return; }
  try {
    const r = await api("/api/tasks", {
      method: "POST",
      body: { urls, save_dir: $("dir-input").value.trim() || undefined, source: "webpage" },
    });
    $("parse-modal").classList.add("hidden");
    $("add-feedback").className = "feedback ok";
    $("add-feedback").textContent = `已加入 ${r.added} 个下载任务${r.skipped ? `，去重跳过 ${r.skipped} 个` : ""}`;
    await loadTasks();
  } catch (e) { alert(e.message); }
}

/* ---------- SSE ---------- */
function connectSSE() {
  const es = new EventSource("/api/events");
  es.onopen = () => { const c = $("conn"); c.textContent = "已连接"; c.className = "conn ok"; };
  es.onerror = () => { const c = $("conn"); c.textContent = "连接断开，重连中…"; c.className = "conn err"; };
  es.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }
    if (msg && msg.type === "task" && msg.task) {
      state.tasks.set(msg.task.id, msg.task);
      refreshSummary();
      render();
    }
  };
}

function refreshSummary() {
  const counts = {};
  for (const t of state.tasks.values()) {
    counts[t.status] = (counts[t.status] || 0) + 1;
  }
  counts.total = state.tasks.size;
  state.summary = counts;
}

function updateRow(t) {
  const row = document.querySelector(`tr[data-id="${t.id}"]`);
  if (!row) return;
  const wasChecked = row.querySelector(".row-check")?.checked || false;
  const tpl = document.createElement("tbody");
  tpl.innerHTML = rowHTML(t);
  const newRow = tpl.firstElementChild;
  if (newRow) {
    newRow.querySelector(".row-check").checked = wasChecked;
    row.replaceWith(newRow);
  }
}

function toast(msg) {
  const fb = $("add-feedback");
  fb.className = "feedback ok"; fb.textContent = msg;
}

/* ---------- 事件绑定 ---------- */
function bindEvents() {
  $("btn-settings").onclick = () => $("settings-panel").classList.toggle("hidden");
  $("btn-save-config").onclick = saveConfig;

  $("btn-browse").onclick = async () => {
    try {
      const r = await api("/api/browse_folder", { method: "POST" });
      if (r.path) $("cfg-dir").value = r.path;
    } catch (e) { alert(e.message); }
  };
  $("btn-open-dir").onclick = async () => {
    const d = $("cfg-dir").value.trim() || state.config.resolved_download_dir;
    try { await api("/api/open", { method: "POST", body: { path: d, is_dir: true } }); }
    catch (e) { alert(e.message); }
  };

  $("btn-add").onclick = addTasks;
  $("btn-parse").onclick = parsePages;
  $("btn-import").onclick = () => $("file-input").click();
  $("file-input").onchange = (e) => { for (const f of e.target.files) importFile(f); e.target.value = ""; };

  $("parse-close").onclick = () => $("parse-modal").classList.add("hidden");
  $("parse-add").onclick = addParsed;

  // 高速下载开关：即时生效
  $("highspeed-switch").onchange = async (e) => {
    try {
      state.config = await api("/api/config", { method: "POST", body: { high_speed: e.target.checked } });
      $("add-feedback").className = "feedback ok";
      $("add-feedback").textContent = e.target.checked ? "已开启高速下载" : "已关闭高速下载";
      await loadTasks();
    } catch (err) {
      alert(err.message);
      e.target.checked = !e.target.checked;
    }
  };

  // 资源分类标签
  $("resource-cats").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-cat]");
    if (!b) return;
    state.resFilter = b.dataset.cat;
    renderResourceCats();
    renderResources();
  });

  // 资源搜索
  let resTimer;
  $("res-search").addEventListener("input", () => {
    clearTimeout(resTimer);
    resTimer = setTimeout(() => { state.resSearch = $("res-search").value; renderResources(); }, 200);
  });

  // 资源排序
  $("res-sort").addEventListener("change", (e) => { state.resSort = e.target.value; renderResources(); });

  // 资源批量选择
  $("res-sel-all").onclick = () => { document.querySelectorAll(".res-check:not(:disabled)").forEach((c) => { c.checked = true; }); };
  $("res-sel-none").onclick = () => { document.querySelectorAll(".res-check").forEach((c) => { c.checked = false; }); };
  $("res-sel-invert").onclick = () => { document.querySelectorAll(".res-check:not(:disabled)").forEach((c) => { c.checked = !c.checked; }); };

  // 筛选
  $("filter-tabs").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-f]");
    if (!b) return;
    document.querySelectorAll("#filter-tabs button").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    state.filter = b.dataset.f;
    state.page = 1;
    render();
  });

  // 搜索
  let searchTimer;
  $("search").addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => { state.search = $("search").value; state.page = 1; render(); }, 200);
  });

  // 分页
  $("prev-page").onclick = () => { if (state.page > 1) { state.page--; renderTable(); } };
  $("next-page").onclick = () => { state.page++; renderTable(); };

  // 全选（仅当前可见行）
  $("check-all").onchange = (e) => {
    document.querySelectorAll(".row-check").forEach((c) => {
      const id = parseInt(c.dataset.id, 10);
      if (e.target.checked) state.selected.add(id);
      else state.selected.delete(id);
      c.checked = e.target.checked;
    });
  };

  // 单行勾选
  $("task-body").addEventListener("change", (e) => {
    if (e.target.classList.contains("row-check")) {
      const id = parseInt(e.target.dataset.id, 10);
      if (e.target.checked) state.selected.add(id);
      else state.selected.delete(id);
    }
  });

  // 表格操作（事件委托）
  $("task-body").addEventListener("click", async (e) => {
    const btn = e.target.closest("button[data-act]");
    if (!btn) return;
    const id = parseInt(btn.dataset.id, 10);
    const t = state.tasks.get(id);
    const actName = btn.dataset.act;
    if (actName === "pause") await act("/api/tasks/pause", [id]);
    else if (actName === "resume") await act("/api/tasks/resume", [id]);
    else if (actName === "cancel") await act("/api/tasks/cancel", [id]);
    else if (actName === "retry") await act("/api/tasks/retry", [id]);
    else if (actName === "open" && t) {
      try { await api("/api/open", { method: "POST", body: { path: t.final_path } }); }
      catch (err) { alert(err.message); }
    }
    else if (actName === "open-dir" && t) {
      try { await api("/api/open", { method: "POST", body: { path: t.final_path, is_dir: true } }); }
      catch (err) { alert(err.message); }
    }
  });

  // 批量操作
  $("btn-pause-sel").onclick = async () => { const ids = selectedIds(); if (ids.length) await act("/api/tasks/pause", ids); };
  $("btn-resume-sel").onclick = async () => { const ids = selectedIds(); if (ids.length) await act("/api/tasks/resume", ids); };
  $("btn-cancel-sel").onclick = async () => { const ids = selectedIds(); if (ids.length) await act("/api/tasks/cancel", ids); };
  $("btn-del-sel").onclick = async () => {
    const ids = selectedIds();
    if (!ids.length) return;
    if (!confirm(`删除选中的 ${ids.length} 个任务记录？`)) return;
    await act("/api/tasks/clear", ids);
    await loadTasks();
  };
  $("btn-pause-all").onclick = async () => { await api("/api/tasks/pause_all", { method: "POST" }); };
  $("btn-resume-all").onclick = async () => { await api("/api/tasks/resume_all", { method: "POST" }); };
  $("btn-retry-all").onclick = async () => {
    try { await api("/api/tasks/retry", { method: "POST", body: { ids: [] } }); }
    catch (e) { alert(e.message); }
  };
  $("btn-clear-done").onclick = async () => {
    if (!confirm("清空所有已完成任务记录？")) return;
    await api("/api/tasks/clear_completed", { method: "POST" });
    await loadTasks();
  };
}

/* ---------- 启动 ---------- */
(async function init() {
  bindEvents();
  try {
    await loadConfig();
    await loadTasks();
  } catch (e) {
    console.error(e);
    $("conn").textContent = "初始化失败";
    $("conn").className = "conn err";
  }
  connectSSE();
})();
