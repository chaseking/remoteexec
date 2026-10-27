"use strict";

const POLL_MS = 3000;
const CATCHUP_MS = 120;       // server still has buffered bytes: come back fast
const ERROR_MS = 6000;        // back off a little after a failed poll
const HIDDEN_MS = 15000;      // background tab: keep polling, just rarely
const FILES_MS = 10000;       // file list refresh while a job runs
const REQUEST_TIMEOUT_MS = 30000;
const MAX_CONSOLE_CHARS = 1000000;  // keep the console node from growing forever
const FLUSH_READS = 3;        // extra reads after a job goes terminal
const DEFAULT_FILTERS = ["running", "finished_1d"];
const IMAGE_EXT = /\.(png|jpe?g|gif|svg|webp|bmp)$/i;

const state = {
  filters: new Set(DEFAULT_FILTERS),
  jobs: [],
  tableTimer: null,
  tableBusy: false,
  tableSig: null,
  detail: null,    // current job row
  // `epoch` is bumped every time the detail view switches jobs or closes. Every
  // response carries the epoch it was issued under and is dropped if it no
  // longer matches, so a reply that arrives after the user navigated away can
  // never be applied to whatever is on screen now.
  epoch: 0,
  logOffset: 0,
  logChars: 0,
  logTimer: null,
  flushLeft: 0,
  stick: true,
  gotLog: false,
  trimmed: false,
  stalled: false,
  skipped: 0,
  logfile: "",   // resolved server-side; array tasks are not named by job id
  tab: "log",
  files: null,
  filesTimer: null,
  selFile: "",
  hosts: { clusters: {}, seen: [] },
  probes: {},    // alias -> {ok, ms, error} | "…"
};

// ---------- theme ----------
function applyTheme(theme, persist) {
  document.documentElement.setAttribute("data-theme", theme);
  if (persist) { try { localStorage.setItem("rxtheme", theme); } catch (e) {} }
}
function initTheme() {
  let stored = null;
  try { stored = localStorage.getItem("rxtheme"); } catch (e) {}
  const mq = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)");
  applyTheme(stored || (mq && mq.matches ? "dark" : "light"), false);
  if (mq && mq.addEventListener) {
    mq.addEventListener("change", (e) => {
      let s = null;
      try { s = localStorage.getItem("rxtheme"); } catch (err) {}
      if (!s) applyTheme(e.matches ? "dark" : "light", false);
    });
  }
}
function toggleTheme() {
  const cur = document.documentElement.getAttribute("data-theme");
  applyTheme(cur === "dark" ? "light" : "dark", true);
}

// ---------- helpers ----------
const $ = (sel) => document.querySelector(sel);
const el = (tag, cls, txt) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (txt != null) e.textContent = txt;
  return e;
};

const RUNNING_STATES = ["RUNNING", "PENDING", "CONFIGURING", "REQUEUED",
  "SUSPENDED", "COMPLETING", "RESIZING", "UNKNOWN"];
const QUEUED_STATES = ["PENDING", "CONFIGURING", "REQUEUED", "SUSPENDED"];

function pillClass(status) {
  const s = (status || "").toUpperCase();
  if (["RUNNING", "COMPLETING", "RESIZING"].includes(s)) return "running";
  if (QUEUED_STATES.includes(s)) return "pending";
  if (s === "COMPLETED") return "completed";
  if (["FAILED", "NODE_FAIL", "OUT_OF_MEMORY", "BOOT_FAIL"].includes(s)) return "failed";
  if (["CANCELLED", "TIMEOUT", "DEADLINE", "PREEMPTED"].includes(s)) return "cancelled";
  return "unknown";
}

function makePill(status) {
  return el("span", "pill " + pillClass(status), status || "UNKNOWN");
}

function formatBytes(n) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${Math.round(n / 1024)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / 1048576).toFixed(1)} MB`;
  return `${(n / 1073741824).toFixed(2)} GB`;
}

// Pretty, locale-relative start time: "8:30am" / "Yesterday, 9:15pm" /
// "Thu, Jun 18, 9:00pm". Returns "—" when unknown.
function formatStarted(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d)) return "—";
  let h = d.getHours();
  const m = d.getMinutes();
  const ap = h < 12 ? "am" : "pm";
  h = h % 12 || 12;
  const time = `${h}:${String(m).padStart(2, "0")}${ap}`;
  const startOfDay = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate());
  const days = Math.round((startOfDay(new Date()) - startOfDay(d)) / 86400000);
  if (days === 0) return time;
  if (days === 1) return `Yesterday, ${time}`;
  const wd = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"][d.getDay()];
  const mo = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep",
    "Oct", "Nov", "Dec"][d.getMonth()];
  return `${wd}, ${mo} ${d.getDate()}, ${time}`;
}

// Every request is bounded: without a timeout a stalled fetch keeps its poll
// loop parked indefinitely, which is what a "stuck on loading" console is.
async function api(path, opts) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), REQUEST_TIMEOUT_MS);
  try {
    const r = await fetch(path, Object.assign({}, opts, { signal: ctl.signal }));
    if (!r.ok) throw new Error(`server returned ${r.status}`);
    return await r.json();
  } finally {
    clearTimeout(timer);
  }
}

function post(path, body) {
  return api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Remoteexec": "1" },
    body: JSON.stringify(body || {}),
  });
}

function toast(msg, ms) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.remove("show"), ms || 1800);
}

// Clipboard API needs a secure context and permission; fall back to a hidden
// textarea so copying also works in embedded web views.
function copyText(val, label) {
  const done = () => toast(label + " copied");
  const fallback = () => {
    const ta = el("textarea");
    ta.value = val || "";
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand("copy"); done(); } catch (e) { toast("Copy failed"); }
    ta.remove();
  };
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(val || "").then(done, fallback);
  } else {
    fallback();
  }
}

function showView(name) {
  for (const v of ["main", "detail", "settings"]) {
    $("#view-" + v).classList.toggle("hidden", v !== name);
  }
}

function setUrl(params) {
  const q = new URLSearchParams(params).toString();
  history.replaceState(null, "", q ? "?" + q : location.pathname);
}

// ---------- table view ----------
async function loadJobs() {
  if (state.tableBusy) return;   // never let two table fetches overlap
  state.tableBusy = true;
  try {
    const q = [...state.filters].join(",");
    const data = await api("/api/jobs?filters=" + encodeURIComponent(q));
    state.jobs = data.jobs || [];
    renderTable();
    renderConnErrors(data.errors || {});
    $("#updated").textContent = "updated " + new Date().toLocaleTimeString();
  } catch (e) {
    $("#updated").textContent = "update failed — retrying";
  } finally {
    state.tableBusy = false;
  }
}

function renderTable() {
  // Rebuilding identical rows every few seconds only costs layout work and
  // drops any text the user was selecting, so skip when nothing changed.
  const sig = JSON.stringify(state.jobs);
  if (sig === state.tableSig) return;
  state.tableSig = sig;

  const body = $("#jobs-body");
  body.replaceChildren();
  $("#empty").classList.toggle("hidden", state.jobs.length > 0);
  const frag = document.createDocumentFragment();
  for (const job of state.jobs) {
    const tr = el("tr");
    const tdStatus = el("td");
    tdStatus.appendChild(makePill(job.status));
    tr.appendChild(tdStatus);
    const name = el("td", "col-name", job.name);
    if (job.command) name.title = job.command;
    tr.appendChild(name);
    tr.appendChild(el("td", "col-path", job.project_path));
    tr.appendChild(el("td", "col-mono", formatStarted(job.started_iso)));
    tr.appendChild(el("td", "col-mono", job.running));
    tr.appendChild(el("td", "col-mono", job.env));
    tr.appendChild(el("td", "col-mono", job.job_id));
    tr.appendChild(el("td", "col-mono", job.host));
    tr.appendChild(el("td", "col-mono", job.partition));
    tr.addEventListener("click", () => openDetail(job));
    frag.appendChild(tr);
  }
  body.appendChild(frag);
}

// An unreachable host would otherwise just look like "no jobs".
function renderConnErrors(errors) {
  const box = $("#conn-error");
  const hosts = Object.keys(errors);
  box.replaceChildren();
  box.classList.toggle("hidden", !hosts.length);
  if (!hosts.length) return;
  const first = errors[hosts[0]].split("\n")[0].slice(0, 160);
  box.appendChild(document.createTextNode(
    `Can't reach ${hosts.join(", ")} — its jobs aren't shown (${first}).`));
  const b = el("button", "linkish", "Switch ssh alias…");
  b.addEventListener("click", openSettings);
  box.appendChild(b);
}

function updateFilterChip() {
  $("#filter-count").textContent = state.filters.size || "";
}

// ---------- console rendering ----------
function setPlaceholder(msg) {
  if (state.gotLog) return;  // real output beats any placeholder
  const c = $("#console");
  c.classList.add("empty-note");
  c.textContent = msg;
}

function setNote(msg) {
  const n = $("#console-note");
  n.textContent = msg || "";
  n.classList.toggle("hidden", !msg);
}

function refreshNote() {
  const parts = [];
  if (state.skipped) {
    parts.push(`earlier output not shown (${formatBytes(state.skipped)} skipped)`);
  } else if (state.trimmed) {
    parts.push("older output trimmed from this view");
  }
  if (state.stalled) {
    parts.push("the log file is shorter than the output already shown — "
      + "reopen this job to reload it");
  }
  if (parts.length && state.logfile) parts.push(`full log: ${state.logfile}`);
  setNote(parts.join(" · "));
}

function appendConsole(text) {
  const c = $("#console");
  if (!state.gotLog) {           // clear the placeholder on the first real bytes
    c.replaceChildren();
    c.classList.remove("empty-note");
    state.gotLog = true;
    state.logChars = 0;
  }
  // A text node append leaves the existing content alone; `textContent +=`
  // would re-serialize and re-lay-out the whole buffer on every poll, which is
  // what makes a long-running console crawl and then hang.
  c.appendChild(document.createTextNode(text));
  state.logChars += text.length;
  while (state.logChars > MAX_CONSOLE_CHARS && c.childNodes.length > 1) {
    state.logChars -= c.firstChild.textContent.length;
    c.removeChild(c.firstChild);
    state.trimmed = true;
  }
  if (state.trimmed) refreshNote();
  if (state.stick) c.scrollTop = c.scrollHeight;
}

function resetConsole() {
  $("#console").replaceChildren();
  state.gotLog = false;
  state.logChars = 0;
  state.trimmed = false;
  state.stalled = false;
  state.skipped = 0;
  setNote("");
  setPlaceholder("Loading…");
}

// Live badge: still producing output (live/queued), or finished for good.
function setLive(st) {
  const b = $("#live");
  const s = (st || "").toUpperCase();
  if (QUEUED_STATES.includes(s)) {
    b.className = "live queued"; b.textContent = "queued";
  } else if (RUNNING_STATES.includes(s)) {
    b.className = "live streaming"; b.textContent = "live";
  } else if (s === "COMPLETED") {
    b.className = "live done"; b.textContent = "complete";
  } else {
    b.className = "live ended"; b.textContent = "ended";
  }
}

function setStatus(st) {
  const pill = $("#detail-status");
  pill.className = "pill " + pillClass(st);
  pill.textContent = st;
  if (state.detail) state.detail.status = st;
}

// ---------- detail view ----------
function openDetail(job) {
  stopLogPolling();          // a second click must not leave a poller behind
  stopFilesPolling();
  stopTablePolling();
  state.epoch += 1;
  state.detail = job;
  state.logOffset = 0;
  state.flushLeft = FLUSH_READS;
  state.stick = true;
  state.logfile = job.logfile || "";
  state.files = null;
  state.selFile = "";
  resetConsole();
  $("#detail-name").textContent = job.name;
  $("#detail-name").title = job.command || "";
  setStatus(job.status);
  setLive(job.status);
  const meta = $("#detail-meta");
  meta.replaceChildren();
  const add = (k, v) => {
    if (!v || v === "—") return;
    const s = el("span");
    s.appendChild(el("b", null, k + " "));
    s.appendChild(document.createTextNode(v));
    meta.appendChild(s);
  };
  add("job", job.job_id);
  add("host", job.host);
  add("partition", job.partition);
  add("env", job.env);
  add("started", formatStarted(job.started_iso));
  add("elapsed", job.running);
  if (job.command) add("cmd", job.command);
  $("#files-count").textContent = "";
  $("#file-view").replaceChildren(el("div", "file-empty", "Select a file to preview"));
  setUrl({ job: job.job_id });
  showView("detail");
  selectTab(state.tab);
  logLoop();
  loadFiles(state.epoch);  // fills the count badge even while on the log tab
}

function closeDetail() {
  stopLogPolling();
  stopFilesPolling();
  state.epoch += 1;          // discard anything still in flight
  state.detail = null;
  $("#menu-list").classList.add("hidden");
  setUrl({});
  showView("main");
  startTablePolling();
}

function selectTab(tab) {
  state.tab = tab;
  document.querySelectorAll(".tab").forEach((b) =>
    b.classList.toggle("active", b.dataset.tab === tab));
  $("#tab-log").classList.toggle("hidden", tab !== "log");
  $("#tab-files").classList.toggle("hidden", tab !== "files");
  if (tab === "files" && state.detail) {
    stopFilesPolling();
    filesLoop();
  }
}

/** One log read. Returns the delay before the next one, or null to stop. */
async function pollLog(epoch) {
  const id = state.detail.job_id;
  let data;
  try {
    data = await api(`/api/jobs/${encodeURIComponent(id)}/logs?offset=${state.logOffset}`);
  } catch (e) {
    if (epoch !== state.epoch) return null;
    setPlaceholder(`Could not read the log (${e.message}) — retrying…`);
    return ERROR_MS;
  }
  if (epoch !== state.epoch) return null;  // user moved on while we waited

  if (data.error) {
    setPlaceholder(`Could not read the log: ${data.error}`);
    return data.error === "unknown job" ? null : ERROR_MS;
  }

  if (data.logfile) state.logfile = data.logfile;
  if (typeof data.next_offset === "number") state.logOffset = data.next_offset;
  if (data.skipped) {
    state.skipped += data.skipped;
    refreshNote();
  }
  if (data.text) appendConsole(data.text);

  const st = (data.state || "UNKNOWN").toUpperCase();
  setStatus(st);

  if (!state.gotLog) {
    setPlaceholder(data.exists
      ? "Waiting for output…"
      : "Waiting for the job to create its log file…");
  }
  // A file that reads as shorter than what we have already shown is never
  // replayed — replaying is how a console ends up rendering the same log
  // twice. Say so instead, and clear the notice if it was a passing blip.
  if (state.stalled !== !!data.stalled) {
    state.stalled = !!data.stalled;
    refreshNote();
  }

  if (data.more) return CATCHUP_MS;   // backlog still draining

  if (!RUNNING_STATES.includes(st)) {
    // Terminal — but the last writes can land after Slurm reports the state, so
    // read a couple more times before giving up on the tail of the output.
    setLive(st);
    if (state.flushLeft > 0 && data.text) state.flushLeft = FLUSH_READS;
    if (state.flushLeft-- > 0) return POLL_MS;
    if (!state.gotLog) {
      setPlaceholder(
        "(no log output found — the remote session directory may have " +
        "been removed, or the job produced no output)");
    }
    if (state.tab === "files") loadFiles(epoch);  // pick up the final files
    return null;
  }
  setLive(st);
  return POLL_MS;
}

// Self-scheduling loop: the next read is only queued once the previous one has
// finished. A fixed setInterval instead lets slow polls overlap, and two reads
// issued at the same offset both return the same bytes — the duplicated lines.
async function logLoop() {
  state.logTimer = null;
  if (!state.detail) return;
  const epoch = state.epoch;
  const next = await pollLog(epoch);
  if (next === null || epoch !== state.epoch || !state.detail) return;
  const delay = document.hidden ? Math.max(next, HIDDEN_MS) : next;
  state.logTimer = setTimeout(logLoop, delay);
}

function stopLogPolling() {
  if (state.logTimer) clearTimeout(state.logTimer);
  state.logTimer = null;
}

// ---------- files ----------
async function loadFiles(epoch) {
  if (!state.detail) return;
  let data;
  try {
    data = await api(`/api/jobs/${encodeURIComponent(state.detail.job_id)}/files`);
  } catch (e) {
    data = { error: e.message, files: [] };
  }
  if (epoch !== state.epoch) return;
  state.files = data;
  $("#files-count").textContent = (data.files && data.files.length) ? data.files.length : "";
  renderFiles();
}

function renderFiles() {
  const data = state.files || { files: [] };
  $("#files-root").textContent = data.root ? `${state.detail.host}:${data.root}` : "";
  $("#files-fetch").disabled = !(data.files && data.files.length);
  const list = $("#files-list");
  list.replaceChildren();
  if (data.error || data.note || !data.files.length) {
    const msg = data.error ? `Could not list files: ${data.error}`
      : data.note ? data.note
      : "No output files yet. Jobs write here via remoteexec.output_dir() "
        + "(or $REMOTEEXEC_OUTPUT_DIR in shell commands).";
    list.appendChild(el("div", "none", msg));
    return;
  }
  let lastDir = null;
  for (const f of data.files) {
    const cut = f.path.lastIndexOf("/");
    const dir = cut >= 0 ? f.path.slice(0, cut) : "";
    const depth = dir ? dir.split("/").length : 0;
    if (dir !== lastDir) {
      if (dir) {
        const d = el("div", "dir", dir + "/");
        d.style.paddingLeft = `${14 + (depth - 1) * 14}px`;
        list.appendChild(d);
      }
      lastDir = dir;
    }
    const row = el("div", "file" + (f.path === state.selFile ? " sel" : ""));
    row.style.paddingLeft = `${14 + depth * 14}px`;
    row.title = f.path;
    row.appendChild(el("span", "fname", cut >= 0 ? f.path.slice(cut + 1) : f.path));
    row.appendChild(el("span", "fsize", formatBytes(f.size)));
    row.addEventListener("click", () => previewFile(f.path));
    list.appendChild(row);
  }
}

function rawUrl(path) {
  return `/api/jobs/${encodeURIComponent(state.detail.job_id)}/file?raw=1&path=${encodeURIComponent(path)}`;
}

async function previewFile(path) {
  state.selFile = path;
  renderFiles();
  const epoch = state.epoch;
  const view = $("#file-view");
  const head = el("div", "file-head");
  head.appendChild(el("span", null, path));
  head.appendChild(el("span", "spacer"));
  const open = el("a", null, "open raw");
  open.href = rawUrl(path);
  open.target = "_blank";
  head.appendChild(open);
  view.replaceChildren(head, el("div", "file-empty", "Loading…"));
  if (IMAGE_EXT.test(path)) {
    const img = el("img");
    img.src = rawUrl(path);
    img.alt = path;
    view.replaceChildren(head, img);
    return;
  }
  let data;
  try {
    data = await api(`/api/jobs/${encodeURIComponent(state.detail.job_id)}/file?path=${encodeURIComponent(path)}`);
  } catch (e) {
    data = { error: e.message };
  }
  if (epoch !== state.epoch || state.selFile !== path) return;
  if (data.error) {
    view.replaceChildren(head, el("div", "file-empty", data.error));
  } else if (data.binary) {
    view.replaceChildren(head, el("div", "file-empty",
      `Binary file (${formatBytes(data.size)}) — use "open raw" or Download all.`));
  } else {
    const pre = el("pre", null, data.text);
    if (data.truncated) {
      head.insertBefore(el("span", "count", `first ${formatBytes(data.text.length)} of ${formatBytes(data.size)}`), open);
    }
    view.replaceChildren(head, pre);
  }
}

async function filesLoop() {
  state.filesTimer = null;
  if (!state.detail || state.tab !== "files") return;
  const epoch = state.epoch;
  await loadFiles(epoch);
  if (epoch !== state.epoch || state.tab !== "files" || !state.detail) return;
  if (RUNNING_STATES.includes((state.detail.status || "").toUpperCase())) {
    state.filesTimer = setTimeout(filesLoop, document.hidden ? HIDDEN_MS : FILES_MS);
  }
}

function stopFilesPolling() {
  if (state.filesTimer) clearTimeout(state.filesTimer);
  state.filesTimer = null;
}

async function fetchAll() {
  if (!state.detail) return;
  const btn = $("#files-fetch");
  btn.disabled = true;
  btn.textContent = "Downloading…";
  try {
    const r = await post(`/api/jobs/${encodeURIComponent(state.detail.job_id)}/fetch`);
    if (r.ok) {
      toast(`Saved to ${r.path}`, 4000);
      post("/api/reveal", { path: r.path }).catch(() => {});
    } else {
      toast("Download failed: " + (r.error || ""), 4000);
    }
  } catch (e) {
    toast("Download failed: " + e.message, 4000);
  } finally {
    btn.disabled = false;
    btn.textContent = "Download all";
  }
}

// ---------- clusters & hosts ----------
async function openSettings() {
  stopTablePolling();
  setUrl({ view: "settings" });
  showView("settings");
  try {
    state.hosts = await api("/api/hosts");
  } catch (e) {
    $("#settings-status").textContent = "could not load: " + e.message;
  }
  renderSettings();
}

function closeSettings() {
  setUrl({});
  showView("main");
  startTablePolling();
}

async function saveHosts(clusters) {
  $("#settings-status").textContent = "saving…";
  try {
    state.hosts = await post("/api/hosts", { clusters });
    $("#settings-status").textContent = "saved " + new Date().toLocaleTimeString();
    state.tableSig = null;  // statuses come through the new route now
  } catch (e) {
    $("#settings-status").textContent = "save failed: " + e.message;
  }
  renderSettings();
}

function cloneClusters() {
  return JSON.parse(JSON.stringify(state.hosts.clusters || {}));
}

async function probe(alias) {
  state.probes[alias] = "…";
  renderSettings();
  try {
    state.probes[alias] = await post("/api/hosts/test", { alias });
  } catch (e) {
    state.probes[alias] = { ok: false, error: e.message };
  }
  renderSettings();
}

function probeBadge(alias) {
  const p = state.probes[alias];
  if (!p) return null;
  if (p === "…") return el("span", "probe", "testing…");
  return el("span", "probe " + (p.ok ? "ok" : "bad"),
    p.ok ? `ok · ${p.ms} ms` : `unreachable${p.error ? " · " + p.error.slice(0, 90) : ""}`);
}

function renderSettings() {
  const box = $("#clusters");
  box.replaceChildren();
  const clusters = state.hosts.clusters || {};
  const names = Object.keys(clusters);
  if (!names.length) {
    box.appendChild(el("p", "hint", "No clusters registered yet — jobs use the ssh host they were submitted with."));
  }
  for (const name of names) {
    const spec = clusters[name];
    const card = el("div", "card");
    const title = el("div", "card-title", name);
    title.appendChild(el("span", "spacer"));
    const rm = el("button", "btn small danger", "Remove");
    rm.addEventListener("click", () => {
      if (!confirm(`Forget cluster ${name}?`)) return;
      const c = cloneClusters();
      delete c[name];
      saveHosts(c);
    });
    title.appendChild(rm);
    card.appendChild(title);

    const useRow = el("div", "row");
    useRow.appendChild(el("span", "label", "connect via"));
    const sel = el("select");
    for (const a of [...spec.hosts, "auto"]) {
      const o = el("option", null, a === "auto" ? "Automatic (first that answers)" : a);
      o.value = a;
      o.selected = a === spec.active;
      sel.appendChild(o);
    }
    sel.addEventListener("change", () => {
      const c = cloneClusters();
      c[name].active = sel.value;
      saveHosts(c);
    });
    useRow.appendChild(sel);
    const testAll = el("button", "btn small", "Test all");
    testAll.addEventListener("click", () => spec.hosts.forEach(probe));
    useRow.appendChild(testAll);
    card.appendChild(useRow);

    const aliasRow = el("div", "row");
    aliasRow.appendChild(el("span", "label", "aliases"));
    for (const a of spec.hosts) {
      const chip = el("span", "alias", a);
      const badge = probeBadge(a);
      if (badge) chip.appendChild(badge);
      const t = el("button", "linkish", "test");
      t.style.fontSize = "11.5px";
      t.addEventListener("click", () => probe(a));
      chip.appendChild(t);
      if (spec.hosts.length > 1) {
        const x = el("button", "x", "×");
        x.title = `remove ${a}`;
        x.addEventListener("click", () => {
          const c = cloneClusters();
          c[name].hosts = c[name].hosts.filter((h) => h !== a);
          saveHosts(c);
        });
        chip.appendChild(x);
      }
      aliasRow.appendChild(chip);
    }
    const inp = el("input");
    inp.placeholder = "add alias";
    inp.spellcheck = false;
    inp.style.width = "140px";
    inp.addEventListener("keydown", (e) => {
      if (e.key !== "Enter" || !inp.value.trim()) return;
      const c = cloneClusters();
      c[name].hosts.push(...inp.value.trim().split(/\s+/));
      saveHosts(c);
    });
    aliasRow.appendChild(inp);
    card.appendChild(aliasRow);
    box.appendChild(card);
  }

  const seen = (state.hosts.seen || []).filter((h) => !(h in clusters));
  const seenBox = $("#seen");
  seenBox.replaceChildren();
  if (seen.length) {
    seenBox.appendChild(document.createTextNode("Hosts your jobs used: "));
    seen.forEach((h, i) => {
      const b = el("button", "linkish", h);
      b.addEventListener("click", () => {
        $("#new-name").value = h;
        $("#new-aliases").value = h + " ";
        $("#new-aliases").focus();
      });
      if (i) seenBox.appendChild(document.createTextNode(", "));
      seenBox.appendChild(b);
    });
  }
}

function addCluster() {
  const name = $("#new-name").value.trim();
  const aliases = $("#new-aliases").value.trim().split(/\s+/).filter(Boolean);
  if (!name) return toast("Give the cluster a name");
  const hosts = aliases.length ? aliases : [name];
  const c = cloneClusters();
  c[name] = { hosts: [...new Set(hosts)], active: hosts[0] };
  $("#new-name").value = "";
  $("#new-aliases").value = "";
  saveHosts(c);
}

// ---------- polling control ----------
async function tableLoop() {
  state.tableTimer = null;
  if (state.detail || $("#view-main").classList.contains("hidden")) return;
  await loadJobs();
  if (!state.detail && !$("#view-main").classList.contains("hidden")) {
    scheduleTable(document.hidden ? HIDDEN_MS : POLL_MS);
  }
}
// Always cancel before arming: a manual refresh landing while a poll is in
// flight would otherwise leave two chained loops running.
function scheduleTable(delay) {
  stopTablePolling();
  state.tableTimer = setTimeout(tableLoop, delay);
}
function startTablePolling() {
  stopTablePolling();
  tableLoop();
}
function stopTablePolling() {
  if (state.tableTimer) clearTimeout(state.tableTimer);
  state.tableTimer = null;
}

// ---------- wire up ----------
async function openFromUrl() {
  const q = new URLSearchParams(location.search);
  if (q.get("view") === "settings") return openSettings();
  const id = q.get("job");
  if (id) {
    try {
      const job = await api(`/api/jobs/${encodeURIComponent(id)}`);
      if (job && job.job_id) return openDetail(job);
    } catch (e) {
      toast(`Job ${id} not found`);
    }
  }
  setUrl({});
  showView("main");
  startTablePolling();
}

function init() {
  initTheme();
  document.querySelectorAll(".theme-toggle").forEach((b) =>
    b.addEventListener("click", (e) => { e.stopPropagation(); toggleTheme(); }));

  const menu = $("#filter-menu");
  menu.querySelectorAll("input").forEach((cb) => {
    cb.checked = state.filters.has(cb.value);
    cb.addEventListener("change", () => {
      if (cb.checked) state.filters.add(cb.value);
      else state.filters.delete(cb.value);
      updateFilterChip();
      startTablePolling();
    });
  });
  updateFilterChip();

  $("#filter-btn").addEventListener("click", (e) => {
    e.stopPropagation();
    menu.classList.toggle("hidden");
  });
  $("#refresh-btn").addEventListener("click", () => startTablePolling());
  $("#settings-btn").addEventListener("click", openSettings);
  $("#settings-back").addEventListener("click", closeSettings);
  $("#back-btn").addEventListener("click", closeDetail);
  $("#add-cluster").addEventListener("click", addCluster);
  $("#new-aliases").addEventListener("keydown", (e) => { if (e.key === "Enter") addCluster(); });
  document.querySelectorAll(".tab").forEach((b) =>
    b.addEventListener("click", () => selectTab(b.dataset.tab)));
  $("#files-refresh").addEventListener("click", () => loadFiles(state.epoch));
  $("#files-fetch").addEventListener("click", fetchAll);

  const mlist = $("#menu-list");
  $("#menu-btn").addEventListener("click", (e) => {
    e.stopPropagation();
    mlist.classList.toggle("hidden");
  });
  const copy = (val, label) => {
    copyText(val, label);
    mlist.classList.add("hidden");
  };
  $("#copy-workdir").addEventListener("click", () =>
    copy(state.detail && state.detail.workdir, "Remote workdir"));
  $("#copy-outdir").addEventListener("click", () =>
    copy(state.detail && state.detail.outdir, "Output dir"));
  $("#copy-logfile").addEventListener("click", () =>
    copy(state.logfile || (state.detail && state.detail.logfile), "Log path"));
  $("#copy-project").addEventListener("click", () =>
    copy(state.detail && state.detail.project_path, "Project path"));
  $("#cancel-job").addEventListener("click", async () => {
    mlist.classList.add("hidden");
    if (!state.detail) return;
    if (!confirm(`Cancel job ${state.detail.job_id} (${state.detail.name})?`)) return;
    try {
      const r = await post(`/api/jobs/${encodeURIComponent(state.detail.job_id)}/cancel`);
      toast(r.ok ? "Cancel requested" : "Cancel failed: " + (r.error || ""));
    } catch (e) {
      toast("Cancel failed: " + e.message);
    }
  });

  document.addEventListener("click", () => {
    menu.classList.add("hidden");
    mlist.classList.add("hidden");
  });

  $("#console").addEventListener("scroll", () => {
    const c = $("#console");
    state.stick = c.scrollHeight - c.scrollTop - c.clientHeight < 30;
  });

  // Coming back to a backgrounded tab: catch up now instead of waiting out the
  // long hidden-tab delay.
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) return;
    if (state.detail) {
      if (!state.logTimer) return;   // stopped on purpose (terminal job)
      stopLogPolling();
      logLoop();
    } else if (!$("#view-main").classList.contains("hidden")) {
      startTablePolling();
    }
  });

  openFromUrl();
}

// If the script is served from cache the document can already be parsed by the
// time this runs, and a DOMContentLoaded listener would never fire.
if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}
