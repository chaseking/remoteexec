"use strict";

const LIST_MS = 4000;         // sidebar refresh
const POLL_MS = 3000;         // log refresh while a job runs
const CATCHUP_MS = 120;       // server still has buffered bytes: come back fast
const ERROR_MS = 6000;        // back off a little after a failed poll
const HIDDEN_MS = 15000;      // background tab: keep polling, just rarely
const FILES_MS = 10000;       // file list refresh while a job runs
const REQUEST_TIMEOUT_MS = 30000;
const MAX_CONSOLE_CHARS = 1000000;  // keep the console node from growing forever
const FLUSH_READS = 3;        // extra reads after a job goes terminal
const IMAGE_EXT = /\.(png|jpe?g|gif|svg|webp|bmp)$/i;
const WINDOWS = [  // key, label, menu label, hours
  ["finished_6h", "6h", "Last 6 hours", 6],
  ["finished_1d", "1d", "Last day", 24],
  ["finished_7d", "7d", "Last 7 days", 168],
  ["all", "all", "Everything", 0],
];

const state = {
  jobs: [],
  errors: {},
  listTimer: null,
  listBusy: false,
  listAgain: false,    // a refresh was asked for while one was in flight
  loadedWindow: null,  // finished-window the current list was fetched with
  listSig: null,
  listLoaded: false,
  finished: stored("rxfinished", "finished_1d"),
  query: "",
  view: "empty",   // "empty" | "job" | "settings"
  detail: null,    // selected job row
  // `epoch` is bumped every time the detail view switches jobs or closes. Every
  // response carries the epoch it was issued under and is dropped if it no
  // longer matches, so a reply that arrives after the user moved on can never
  // be applied to whatever is on screen now.
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

// ---------- icons (SF Symbols look-alikes) ----------
const S = (body, extra) =>
  `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"${extra || ""}>${body}</svg>`;
const ICONS = {
  terminal: S('<rect x="3" y="4" width="18" height="16" rx="3"/><path d="M7.5 9.5l3 2.5-3 2.5M12.5 15h4"/>'),
  refresh: S('<path d="M20.5 12a8.5 8.5 0 1 1-2.5-6"/><path d="M20.5 3.5V9H15"/>'),
  search: S('<circle cx="11" cy="11" r="6.5"/><path d="M20 20l-4.2-4.2"/>'),
  globe: S('<circle cx="12" cy="12" r="8.5"/><path d="M3.5 12h17M12 3.5c2.6 2.4 3.8 5.3 3.8 8.5s-1.2 6.1-3.8 8.5c-2.6-2.4-3.8-5.3-3.8-8.5s1.2-6.1 3.8-8.5z"/>'),
  chevron: S('<path d="M6 9l6 6 6-6"/>', ' stroke-width="2.4"'),
  gear: S('<rect x="3.5" y="4" width="17" height="7" rx="2"/><rect x="3.5" y="13" width="17" height="7" rx="2"/><path d="M7.5 7.5h.01M7.5 16.5h.01"/>'),
  moon: S('<path d="M20.5 13.5A8.5 8.5 0 1 1 10.5 3.5a6.6 6.6 0 0 0 10 10z"/>'),
  sun: S('<circle cx="12" cy="12" r="4"/><path d="M12 2.5v2M12 19.5v2M4.6 4.6l1.4 1.4M18 18l1.4 1.4M2.5 12h2M19.5 12h2M4.6 19.4L6 18M18 6l1.4-1.4"/>'),
  back: S('<path d="M15 18l-6-6 6-6"/>', ' stroke-width="2.2"'),
  download: S('<path d="M12 4v11M7 10.5l5 5 5-5M5 20h14"/>', ' stroke-width="2"'),
  more: S('<circle cx="5.5" cy="12" r="1.4" fill="currentColor"/><circle cx="12" cy="12" r="1.4" fill="currentColor"/><circle cx="18.5" cy="12" r="1.4" fill="currentColor"/>'),
  down: S('<path d="M12 5v14M6 13l6 6 6-6"/>', ' stroke-width="2.4"'),
  warn: '<svg viewBox="0 0 24 24"><path d="M10.3 3.8L2.4 18a2 2 0 0 0 1.7 3h15.8a2 2 0 0 0 1.7-3L13.7 3.8a2 2 0 0 0-3.4 0z" fill="currentColor"/><path d="M12 9v5M12 17.3h.01" stroke="#fff" stroke-width="2" stroke-linecap="round"/></svg>',
  file: S('<path d="M14 3.5H7.5a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2h9a2 2 0 0 0 2-2V8z"/><path d="M14 3.5V8h4.5"/>'),
  image: S('<rect x="3.5" y="4.5" width="17" height="15" rx="2.5"/><circle cx="9" cy="10" r="1.8"/><path d="M20.5 16l-4.5-4.5-8.5 8"/>'),
  copy: S('<rect x="8.5" y="8.5" width="11" height="11" rx="2"/><path d="M15.5 8.5V6.5a2 2 0 0 0-2-2h-7a2 2 0 0 0-2 2v7a2 2 0 0 0 2 2h2"/>'),
};
const ST = {
  running: '<svg viewBox="0 0 16 16"><circle cx="8" cy="8" r="6" fill="currentColor"/></svg>',
  queued: '<svg viewBox="0 0 16 16"><circle cx="8" cy="8" r="5.6" fill="none" stroke="currentColor" stroke-width="1.6" stroke-dasharray="2.3 2"/></svg>',
  completed: '<svg viewBox="0 0 16 16"><circle cx="8" cy="8" r="7" fill="currentColor"/><path d="M4.9 8.3l2.1 2.1 4.1-4.6" fill="none" stroke="#fff" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  failed: '<svg viewBox="0 0 16 16"><circle cx="8" cy="8" r="7" fill="currentColor"/><path d="M5.6 5.6l4.8 4.8M10.4 5.6l-4.8 4.8" stroke="#fff" stroke-width="1.7" stroke-linecap="round"/></svg>',
  stopped: '<svg viewBox="0 0 16 16"><circle cx="8" cy="8" r="7" fill="currentColor"/><path d="M5 8h6" stroke="#fff" stroke-width="1.8" stroke-linecap="round"/></svg>',
  unknown: '<svg viewBox="0 0 16 16"><circle cx="8" cy="8" r="6.3" fill="none" stroke="currentColor" stroke-width="1.4"/><path d="M6.4 6.4a1.7 1.7 0 1 1 2.4 1.6c-.5.3-.8.6-.8 1.2M8 11.4h.01" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg>',
};

function icon(name, cls) {
  const s = document.createElement("span");
  s.className = "ico" + (cls ? " " + cls : "");
  s.innerHTML = ICONS[name] || "";
  return s;
}
function statusIcon(status) {
  const ph = phase(status);
  const s = document.createElement("span");
  s.className = "st " + ph;
  s.innerHTML = ST[ph];
  return s;
}
function paintIcons(root) {
  (root || document).querySelectorAll(".ico[data-icon]").forEach((e) => {
    if (e.dataset.icon !== "theme") e.innerHTML = ICONS[e.dataset.icon] || "";
  });
  paintThemeIcon();
}

// ---------- helpers ----------
const $ = (sel) => document.querySelector(sel);
const el = (tag, cls, txt) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (txt != null) e.textContent = txt;
  return e;
};

function stored(key, fallback) {
  try { return localStorage.getItem(key) || fallback; } catch (e) { return fallback; }
}
function store(key, val) {
  try { localStorage.setItem(key, val); } catch (e) {}
}

// Slurm states that mean "keep reading the log" (UNKNOWN: not known yet).
const RUNNING_STATES = ["RUNNING", "PENDING", "CONFIGURING", "REQUEUED",
  "SUSPENDED", "COMPLETING", "RESIZING", "UNKNOWN"];

function phase(status) {
  const s = (status || "").toUpperCase();
  if (["RUNNING", "COMPLETING", "CONFIGURING", "RESIZING"].includes(s)) return "running";
  if (["PENDING", "REQUEUED", "SUSPENDED"].includes(s)) return "queued";
  if (s === "COMPLETED") return "completed";
  if (["FAILED", "OUT_OF_MEMORY", "NODE_FAIL", "BOOT_FAIL"].includes(s)) return "failed";
  if (["CANCELLED", "TIMEOUT", "DEADLINE", "PREEMPTED"].includes(s)) return "stopped";
  return "unknown";
}
const isActive = (status) => ["running", "queued"].includes(phase(status));
const present = (v) => v && v !== "—";

function statusLabel(status) {
  const s = (status || "UNKNOWN").toLowerCase().replace(/_/g, " ");
  return s.charAt(0).toUpperCase() + s.slice(1);
}

function formatBytes(n) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${Math.round(n / 1024)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / 1048576).toFixed(1)} MB`;
  return `${(n / 1073741824).toFixed(2)} GB`;
}

function parseTime(s) {
  if (!s || s.length < 19) return null;
  const d = new Date(s.slice(0, 19));  // no zone: local time, like the cluster's
  return isNaN(d) ? null : d;
}

function ago(s) {
  const d = parseTime(s);
  if (!d) return "";
  const secs = Math.max(0, Math.floor((Date.now() - d) / 1000));
  if (secs < 60) return "just now";
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
  return `${Math.floor(secs / 86400)}d ago`;
}

// "8:30am" / "Yesterday, 9:15pm" / "Thu, Jun 18, 9:00pm"
function formatStarted(iso) {
  const d = parseTime(iso);
  if (!d) return "";
  let h = d.getHours();
  const ap = h < 12 ? "am" : "pm";
  h = h % 12 || 12;
  const time = `${h}:${String(d.getMinutes()).padStart(2, "0")}${ap}`;
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

function setUrl(params) {
  const q = new URLSearchParams(params).toString();
  history.replaceState(null, "", q ? "?" + q : location.pathname);
}

function closeMenus(except) {
  document.querySelectorAll(".popmenu").forEach((m) => { if (m !== except) m.classList.add("hidden"); });
}
function toggleMenu(menu, e) {
  e.stopPropagation();
  const open = menu.classList.contains("hidden");
  closeMenus(menu);
  menu.classList.toggle("hidden", !open);
  return open;
}

// ---------- theme ----------
function applyTheme(theme, persist) {
  document.documentElement.setAttribute("data-theme", theme);
  if (persist) store("rxtheme", theme);
  paintThemeIcon();
}
function paintThemeIcon() {
  const b = document.querySelector('.ico[data-icon="theme"]');
  if (b) b.innerHTML = document.documentElement.getAttribute("data-theme") === "dark" ? ICONS.sun : ICONS.moon;
}
function initTheme() {
  const mq = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)");
  const saved = stored("rxtheme", "");
  applyTheme(saved || (mq && mq.matches ? "dark" : "light"), false);
  if (mq && mq.addEventListener) {
    mq.addEventListener("change", (e) => {
      if (!stored("rxtheme", "")) applyTheme(e.matches ? "dark" : "light", false);
    });
  }
}

// ---------- sidebar ----------
async function loadJobs() {
  // Never let two list fetches overlap, but don't drop the second one either:
  // it may be for a different finished-window.
  if (state.listBusy) {
    state.listAgain = true;
    return;
  }
  state.listBusy = true;
  try {
    const win = state.finished;
    const filters = win === "all" ? "" : `running,${win}`;
    const data = await api("/api/jobs?filters=" + encodeURIComponent(filters));
    if (win !== state.finished) return;  // superseded while in flight
    state.loadedWindow = win;
    state.jobs = data.jobs || [];
    state.errors = data.errors || {};
    state.listLoaded = true;
    $("#updated").textContent = new Date().toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    $("#updated").title = "Last updated";
    syncDetailFromList();
    renderSidebar();
    if (state.view === "empty") autoSelect();
  } catch (e) {
    $("#updated").textContent = "offline";
    $("#updated").title = "Update failed — retrying";
  } finally {
    state.listBusy = false;
    if (state.listAgain) {
      state.listAgain = false;
      loadJobs();
    }
  }
}

function windowHours(key) {
  return (WINDOWS.find((w) => w[0] === key) || [])[3] || 0;
}

// Still fetching jobs for a wider finished-window than the list holds.
function windowLoading() {
  const want = windowHours(state.finished), have = windowHours(state.loadedWindow);
  return state.loadedWindow !== null && state.finished !== state.loadedWindow
    && (want === 0 || (have !== 0 && want > have));
}

function visibleJobs() {
  const q = state.query.trim().toLowerCase();
  const hit = (j) => !q || [j.name, j.job_id, j.host, j.command, j.partition, j.env]
    .some((v) => (v || "").toLowerCase().includes(q));
  const jobs = state.jobs.filter(hit);
  const active = jobs.filter((j) => isActive(j.status));
  // A narrower window than the one fetched is applied here, without waiting.
  const hours = windowHours(state.finished);
  const cutoff = Date.now() - hours * 3600000;
  const finished = jobs.filter((j) => !isActive(j.status))
    .filter((j) => !hours || state.loadedWindow === state.finished
      || (parseTime(j.end) || 0) >= cutoff)
    .sort((a, b) => (b.end || b.submitted_at || "").localeCompare(a.end || a.submitted_at || ""));
  return { active, finished };
}

function timeText(job) {
  const ph = phase(job.status);
  if (ph === "queued") return "queued";
  if (ph === "running") return present(job.running) ? job.running : "";
  return ago(job.end) || statusLabel(job.status);
}

function makeRow(job) {
  const b = el("button", "row");
  b.dataset.id = job.job_id;
  if (state.detail && state.detail.job_id === job.job_id && state.view === "job") b.classList.add("sel");
  b.appendChild(statusIcon(job.status));
  const text = el("span", "text");
  text.appendChild(el("span", "name", job.name));
  const sub = [job.job_id, job.host];
  if (present(job.partition)) sub.push(job.partition);
  text.appendChild(el("span", "sub", sub.join(" · ")));
  b.appendChild(text);
  b.appendChild(el("span", "time" + (isActive(job.status) ? " active" : ""), timeText(job)));
  b.title = job.command || job.name;
  b.addEventListener("click", () => openJob(job));
  return b;
}

function sectionHeader(title, count, extra) {
  const h = el("div", "section");
  h.appendChild(el("span", null, title));
  h.appendChild(el("span", "n", String(count)));
  if (extra) h.appendChild(extra);
  return h;
}

function windowPicker() {
  const wrap = el("div", "pop-wrap");
  const cur = WINDOWS.find((w) => w[0] === state.finished) || WINDOWS[1];
  const btn = el("button", "section-btn", cur[1]);
  btn.appendChild(icon("chevron", "small"));
  btn.title = "How far back to show finished jobs";
  const menu = el("div", "popmenu right hidden");
  for (const [key, , label] of WINDOWS) {
    const item = el("button");
    item.appendChild(el("span", "check", key === state.finished ? "✓" : ""));
    item.appendChild(el("span", null, label));
    item.addEventListener("click", () => {
      closeMenus();
      if (key === state.finished) return;
      state.finished = key;
      store("rxfinished", key);
      state.listSig = null;
      renderSidebar();      // new label + spinner (or the narrower list) now
      startListPolling();
    });
    menu.appendChild(item);
  }
  btn.addEventListener("click", (e) => toggleMenu(menu, e));
  if (windowLoading()) {
    const spin = el("span", "spinner");
    spin.title = "Loading jobs…";
    wrap.appendChild(spin);
  }
  wrap.appendChild(btn);
  wrap.appendChild(menu);
  return wrap;
}

function renderSidebar() {
  renderConnErrors();
  const { active, finished } = visibleJobs();
  // Rebuilding identical rows only costs layout work (and closes an open
  // menu), so skip it when nothing visible changed. The minute is part of the
  // signature so "5m ago" keeps moving.
  const sig = JSON.stringify([active, finished, state.view === "job" && state.detail && state.detail.job_id,
    state.finished, windowLoading(), Math.floor(Date.now() / 60000)]);
  if (sig === state.listSig) return;
  state.listSig = sig;

  const list = $("#job-list");
  const scroll = list.scrollTop;
  list.replaceChildren();
  if (active.length) {
    list.appendChild(sectionHeader("Running", active.length));
    active.forEach((j) => list.appendChild(makeRow(j)));
  }
  list.appendChild(sectionHeader("Finished", finished.length, windowPicker()));
  finished.forEach((j) => list.appendChild(makeRow(j)));
  if (!active.length && !finished.length) {
    list.appendChild(el("div", "none", state.query
      ? "No jobs match your search."
      : state.listLoaded ? "No jobs in this window." : "Loading…"));
  }
  list.scrollTop = scroll;
}

// An unreachable host would otherwise just look like "no jobs".
function renderConnErrors() {
  const box = $("#conn-error");
  const hosts = Object.keys(state.errors);
  box.classList.toggle("hidden", !hosts.length);
  if (!hosts.length) return;
  box.replaceChildren(icon("warn"));
  const text = el("div");
  const b = el("b", null, `Can't reach ${hosts.join(", ")}`);
  text.appendChild(b);
  text.appendChild(el("div", "muted", state.errors[hosts[0]].split("\n")[0].slice(0, 140)));
  const sw = el("button", "linkish", "Switch ssh alias…");
  sw.addEventListener("click", openSettings);
  text.appendChild(sw);
  box.appendChild(text);
}

function selectRelative(step) {
  const { active, finished } = visibleJobs();
  const all = [...active, ...finished];
  if (!all.length) return;
  const i = state.detail ? all.findIndex((j) => j.job_id === state.detail.job_id) : -1;
  const next = all[Math.min(all.length - 1, Math.max(0, i + step))];
  if (next && (!state.detail || next.job_id !== state.detail.job_id)) {
    openJob(next);
    const row = document.querySelector(`.row[data-id="${CSS.escape(next.job_id)}"]`);
    if (row) row.scrollIntoView({ block: "nearest" });
  }
}

// Opened without a job: show the newest one rather than an empty pane.
function autoSelect() {
  if (window.innerWidth <= 760) return;
  const { active, finished } = visibleJobs();
  const first = active[0] || finished[0];
  if (first) openJob(first);
  else showEmpty();
}

// ---------- route switcher ----------
async function loadHosts() {
  try {
    state.hosts = await api("/api/hosts");
  } catch (e) { /* keep the last known */ }
  renderRouteLabel();
}

function renderRouteLabel() {
  const clusters = state.hosts.clusters || {};
  const names = Object.keys(clusters).sort();
  $("#route-label").textContent = names.length
    ? names.map((n) => (clusters[n].active === n ? n : `${n} → ${clusters[n].active}`)).join(", ")
    : "hosts";
}

function renderRouteMenu() {
  const menu = $("#route-menu");
  menu.replaceChildren();
  const clusters = state.hosts.clusters || {};
  for (const name of Object.keys(clusters).sort()) {
    const spec = clusters[name];
    menu.appendChild(el("div", "label", name));
    for (const alias of [...spec.hosts, "auto"]) {
      const item = el("button");
      item.appendChild(el("span", "check", spec.active === alias ? "✓" : ""));
      item.appendChild(el("span", null, alias === "auto" ? "Automatic" : alias));
      if (alias === "auto") item.title = "Use the first alias that answers";
      item.addEventListener("click", () => {
        const c = cloneClusters();
        c[name].active = alias;
        saveHosts(c);
      });
      menu.appendChild(item);
    }
  }
  if (Object.keys(clusters).length) menu.appendChild(el("div", "sep"));
  const manage = el("button", null, "Manage clusters & ssh hosts…");
  manage.addEventListener("click", openSettings);
  menu.appendChild(manage);
}

// ---------- views ----------
function showPane(name) {
  state.view = name;
  for (const v of ["empty", "job", "settings"]) {
    $("#view-" + v).classList.toggle("hidden", v !== name);
  }
  $("#app").classList.toggle("has-detail", name !== "empty");
}

function leaveJob() {
  stopLogPolling();
  stopFilesPolling();
  state.epoch += 1;          // discard anything still in flight
  closeMenus();
}

function showEmpty() {
  leaveJob();
  state.detail = null;
  const n = state.jobs.length;
  $("#empty-title").textContent = n ? "Pick a job" : "No jobs yet";
  $("#empty-sub").textContent = n
    ? "Select a job on the left to follow its log and browse its output files."
    : "Jobs you submit show up here with live logs and output files.";
  setUrl({});
  showPane("empty");
  state.listSig = null;
  renderSidebar();
}

// ---------- job detail ----------
function openJob(job) {
  leaveJob();
  state.detail = Object.assign({}, job);
  state.logOffset = 0;
  state.flushLeft = FLUSH_READS;
  state.stick = true;
  state.logfile = job.logfile || "";
  state.files = null;
  state.selFile = "";
  resetConsole();
  renderJobHeader();
  renderDetails();
  $("#files-count").textContent = "";
  $("#download-btn").disabled = !job.outdir;
  $("#file-view").replaceChildren(el("div", "file-empty", "Select a file to preview it"));
  $("#files-list").replaceChildren();
  setUrl({ job: job.job_id });
  showPane("job");
  state.listSig = null;
  renderSidebar();
  selectTab(state.tab);
  logLoop();
  loadFiles(state.epoch);  // fills the count badge even while on the log tab
}

// Keep the header's elapsed time / state current from the list refresh.
function syncDetailFromList() {
  if (!state.detail || state.view !== "job") return;
  const fresh = state.jobs.find((j) => j.job_id === state.detail.job_id);
  if (!fresh) return;
  const keepStatus = state.detail.status;
  Object.assign(state.detail, fresh);
  if (phase(keepStatus) !== "unknown" && phase(fresh.status) === "unknown") state.detail.status = keepStatus;
  renderStatusRow();
}

function renderJobHeader() {
  const j = state.detail;
  $("#job-title").textContent = j.name;
  $("#job-title").title = j.command || j.name;
  renderStatusRow();
}

function renderStatusRow() {
  const j = state.detail;
  if (!j) return;
  const row = $("#status-row");
  row.replaceChildren();
  const ph = phase(j.status);
  const st = el("span", "state");
  st.appendChild(statusIcon(j.status));
  st.appendChild(el("span", null, statusLabel(j.status)));
  row.appendChild(st);

  let live;
  if (ph === "queued") live = "waiting to start";
  else if (ph === "running") live = "live log";
  else live = "";  // finished: the label says how; add the exit code below
  if (ph !== "running" && ph !== "queued" && present(j.exitcode)) {
    live += (live ? " · " : "") + `exit ${j.exitcode.split(":")[0]}`;
  }
  if (live) row.appendChild(el("span", "live", live));

  const meta = [];
  meta.push(`#${j.job_id}`, j.host);
  if (present(j.partition)) meta.push(j.partition);
  if (present(j.gres)) meta.push(`gres ${j.gres}`);
  if (present(j.env)) meta.push(j.env);
  const started = formatStarted(j.started_iso);
  if (started) meta.push(`started ${started}`);
  const m = el("span", "meta", meta.join(" · "));
  if (present(j.running)) {
    m.appendChild(document.createTextNode(" · "));
    m.appendChild(el("b", null, j.running));
    m.appendChild(document.createTextNode(isActive(j.status) ? " elapsed" : " run time"));
  }
  row.appendChild(m);
  $("#cancel-btn").classList.toggle("hidden", !isActive(j.status));
}

function setStatus(st) {
  if (!state.detail) return;
  const changed = state.detail.status !== st;
  state.detail.status = st;
  if (changed) {
    renderStatusRow();
    const local = state.jobs.find((j) => j.job_id === state.detail.job_id);
    if (local) {
      local.status = st;
      renderSidebar();
    }
  }
}

function selectTab(tab) {
  state.tab = tab;
  document.querySelectorAll(".seg-btn").forEach((b) =>
    b.classList.toggle("active", b.dataset.tab === tab));
  for (const t of ["log", "files", "details"]) {
    $("#tab-" + t).classList.toggle("hidden", t !== tab);
  }
  stopFilesPolling();
  if (tab === "files" && state.detail) filesLoop();
  if (tab === "details" && state.detail) loadScript(state.epoch);
}

// ---------- console ----------
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
    parts.push(`Earlier output not shown (${formatBytes(state.skipped)} skipped)`);
  } else if (state.trimmed) {
    parts.push("Older output trimmed from this view");
  }
  if (state.stalled) {
    parts.push("The log file reads shorter than what is shown — reopen the job to reload it");
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
  else $("#jump-btn").classList.remove("hidden");
}

function resetConsole() {
  $("#console").replaceChildren();
  state.gotLog = false;
  state.logChars = 0;
  state.trimmed = false;
  state.stalled = false;
  state.skipped = 0;
  $("#jump-btn").classList.add("hidden");
  setNote("");
  setPlaceholder("Loading…");
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
      : phase(st) === "queued" ? "Waiting for the job to start…"
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
    if (state.flushLeft > 0 && data.text) state.flushLeft = FLUSH_READS;
    if (state.flushLeft-- > 0) {
      renderStatusRow();
      return POLL_MS;
    }
    renderStatusRow();
    if (!state.gotLog) {
      setPlaceholder(
        "No log output — the job printed nothing, or its run directory was removed.");
    }
    if (state.tab === "files") loadFiles(epoch);  // pick up the final files
    return null;
  }
  return POLL_MS;
}

// Self-scheduling loop: the next read is only queued once the previous one has
// finished. A fixed setInterval instead lets slow polls overlap, and two reads
// issued at the same offset both return the same bytes — the duplicated lines.
async function logLoop() {
  state.logTimer = null;
  if (!state.detail || state.view !== "job") return;
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
  const n = (data.files || []).length;
  $("#files-count").textContent = n ? String(n) : "";
  $("#download-btn").disabled = !n;
  renderFiles();
}

function renderFiles() {
  const data = state.files || { files: [] };
  $("#files-root").textContent = data.root ? `${state.detail.host}:${data.root}` : "";
  $("#files-root").title = $("#files-root").textContent;
  const list = $("#files-list");
  list.replaceChildren();
  if (data.error || data.note || !data.files.length) {
    const msg = data.error ? `Could not list files: ${data.error}`
      : data.note ? data.note
      : "No output files yet. Jobs write here with remoteexec.output_dir() "
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
        d.style.paddingLeft = `${10 + (depth - 1) * 14}px`;
        list.appendChild(d);
      }
      lastDir = dir;
    }
    const row = el("button", "row file" + (f.path === state.selFile ? " sel" : ""));
    row.style.paddingLeft = `${10 + depth * 14}px`;
    row.title = f.path;
    row.appendChild(icon(IMAGE_EXT.test(f.path) ? "image" : "file"));
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
  head.appendChild(el("span", "path", path));
  head.appendChild(el("span", "spacer"));
  const open = el("a", null, "Open raw");
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
      `Binary file (${formatBytes(data.size)}) — use “Open raw” or Download outputs.`));
  } else {
    if (data.truncated) {
      head.insertBefore(el("span", "count", `first ${formatBytes(data.text.length)} of ${formatBytes(data.size)}`), open);
    }
    view.replaceChildren(head, el("pre", null, data.text));
  }
}

async function filesLoop() {
  state.filesTimer = null;
  if (!state.detail || state.tab !== "files") return;
  const epoch = state.epoch;
  await loadFiles(epoch);
  if (epoch !== state.epoch || state.tab !== "files" || !state.detail) return;
  if (isActive(state.detail.status)) {
    state.filesTimer = setTimeout(filesLoop, document.hidden ? HIDDEN_MS : FILES_MS);
  }
}

function stopFilesPolling() {
  if (state.filesTimer) clearTimeout(state.filesTimer);
  state.filesTimer = null;
}

async function downloadOutputs() {
  if (!state.detail) return;
  const btn = $("#download-btn");
  btn.disabled = true;
  try {
    toast("Downloading outputs…", 60000);
    const r = await post(`/api/jobs/${encodeURIComponent(state.detail.job_id)}/fetch`);
    if (r.ok) {
      toast(`Saved to ${r.path}`, 4000);
      post("/api/reveal", { path: r.path }).catch(() => {});
    } else {
      toast("Download failed: " + (r.error || ""), 5000);
    }
  } catch (e) {
    toast("Download failed: " + e.message, 5000);
  } finally {
    btn.disabled = false;
  }
}

// ---------- details ----------
function renderDetails() {
  const j = state.detail;
  const grid = $("#details-grid");
  grid.replaceChildren();
  const rows = [
    ["Status", statusLabel(j.status)],
    ["Job ID", j.job_id, true],
    ["Name", j.name],
    ["Command", j.command, true, true],
    ["Host", j.host],
    ["Partition", j.partition],
    ["GRES", j.gres, true],
    ["Environment", j.env],
    ["Submitted", formatStarted(j.submitted_at)],
    ["Started", formatStarted(j.started_iso)],
    [isActive(j.status) ? "Elapsed" : "Run time", j.running],
    ["Exit code", j.exitcode],
    ["Local project", j.project_path, true, true],
    ["Remote workdir", j.workdir, true, true],
    ["Output dir", j.outdir, true, true],
    ["Log file", state.logfile || j.logfile, true, true],
  ];
  for (const [k, v, mono, copyable] of rows) {
    if (!present(v)) continue;
    grid.appendChild(el("dt", null, k));
    const dd = el("dd");
    dd.appendChild(el("span", "v" + (mono ? " mono" : ""), v));
    if (copyable) {
      const c = el("button", "icon-btn copy");
      c.title = "Copy";
      c.appendChild(icon("copy"));
      c.addEventListener("click", () => copyText(v, k));
      dd.appendChild(c);
    }
    grid.appendChild(dd);
  }
}

async function loadScript(epoch) {
  renderDetails();
  const pre = $("#script");
  pre.textContent = "Loading…";
  let data;
  try {
    data = await api(`/api/jobs/${encodeURIComponent(state.detail.job_id)}/script`);
  } catch (e) {
    data = { error: e.message };
  }
  if (epoch !== state.epoch) return;
  pre.classList.toggle("muted", !!data.error);
  pre.textContent = data.error ? `(not available: ${data.error})` : data.text;
  // Jobs submitted before GRES was recorded: read it from the script.
  const gres = !data.error && !present(state.detail.gres) &&
    [...data.text.matchAll(/^#SBATCH\s+--gres[= ](\S+)/gm)].map((m) => m[1]).join(",");
  if (gres) {
    state.detail.gres = gres;
    renderDetails();
    renderStatusRow();
  }
}

// ---------- clusters & hosts ----------
async function openSettings() {
  leaveJob();
  closeMenus();
  setUrl({ view: "settings" });
  showPane("settings");
  state.listSig = null;
  renderSidebar();
  await loadHosts();
  renderSettings();
}

function backToJobs() {
  const { active, finished } = visibleJobs();
  const first = active[0] || finished[0];
  if (first && window.innerWidth > 760) openJob(first);
  else showEmpty();
}

async function saveHosts(clusters) {
  closeMenus();
  $("#settings-status").textContent = "Saving…";
  try {
    state.hosts = await post("/api/hosts", { clusters });
    $("#settings-status").textContent = "Saved";
    state.listSig = null;
    startListPolling();  // statuses come through the new route now
  } catch (e) {
    $("#settings-status").textContent = "Save failed: " + e.message;
  }
  renderRouteLabel();
  if (state.view === "settings") renderSettings();
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
    p.ok ? `ok · ${p.ms} ms` : `unreachable${p.error ? " · " + p.error.slice(0, 80) : ""}`);
}

function renderSettings() {
  const box = $("#clusters");
  box.replaceChildren();
  const clusters = state.hosts.clusters || {};
  const names = Object.keys(clusters).sort();
  if (!names.length) {
    const g = el("div", "group");
    g.appendChild(el("div", "muted", "No clusters registered yet — jobs use the ssh host they were submitted with."));
    box.appendChild(g);
  }
  for (const name of names) {
    const spec = clusters[name];
    const g = el("div", "group");
    const title = el("div", "group-title", name);
    title.appendChild(el("span", "spacer"));
    const rm = el("button", "btn danger", "Remove");
    rm.addEventListener("click", () => {
      if (!confirm(`Forget cluster ${name}?`)) return;
      const c = cloneClusters();
      delete c[name];
      saveHosts(c);
    });
    title.appendChild(rm);
    g.appendChild(title);

    const use = el("div", "row-line");
    use.appendChild(el("span", "label", "Connect via"));
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
    use.appendChild(sel);
    const testAll = el("button", "btn", "Test all");
    testAll.addEventListener("click", () => spec.hosts.forEach(probe));
    use.appendChild(testAll);
    g.appendChild(use);

    const aliases = el("div", "row-line");
    aliases.appendChild(el("span", "label", "Aliases"));
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
        x.title = `Remove ${a}`;
        x.addEventListener("click", () => {
          const c = cloneClusters();
          c[name].hosts = c[name].hosts.filter((h) => h !== a);
          saveHosts(c);
        });
        chip.appendChild(x);
      }
      aliases.appendChild(chip);
    }
    const inp = el("input");
    inp.placeholder = "add alias";
    inp.spellcheck = false;
    inp.style.width = "130px";
    inp.addEventListener("keydown", (e) => {
      if (e.key !== "Enter" || !inp.value.trim()) return;
      const c = cloneClusters();
      c[name].hosts.push(...inp.value.trim().split(/\s+/));
      saveHosts(c);
    });
    aliases.appendChild(inp);
    g.appendChild(aliases);
    box.appendChild(g);
  }

  const seen = (state.hosts.seen || []).filter((h) => !(h in clusters));
  const seenBox = $("#seen");
  seenBox.replaceChildren();
  if (seen.length) {
    seenBox.style.marginTop = "10px";
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
async function listLoop() {
  state.listTimer = null;
  await loadJobs();
  state.listTimer = setTimeout(listLoop, document.hidden ? HIDDEN_MS : LIST_MS);
}
// Always cancel before arming: a manual refresh landing while a poll is in
// flight would otherwise leave two chained loops running.
function startListPolling() {
  if (state.listTimer) clearTimeout(state.listTimer);
  state.listTimer = null;
  listLoop();
}

// ---------- wire up ----------
async function openFromUrl() {
  const q = new URLSearchParams(location.search);
  if (q.get("view") === "settings") return openSettings();
  const id = q.get("job");
  if (id) {
    try {
      const job = await api(`/api/jobs/${encodeURIComponent(id)}`);
      if (job && job.job_id) return openJob(job);
    } catch (e) {
      toast(`Job ${id} not found`);
    }
  }
  setUrl({});
}

function init() {
  // Set by the macOS app: the page then sits under its transparent titlebar,
  // which shows the window title, so the sidebar's own header is hidden.
  if (window.remoteexecApp) {
    document.documentElement.classList.add("in-app");
    const inset = window.remoteexecApp.topInset;
    if (typeof inset === "number") {
      document.documentElement.style.setProperty("--top-inset", `${inset}px`);
    }
  }
  initTheme();
  paintIcons();

  $("#theme-btn").addEventListener("click", () => {
    const cur = document.documentElement.getAttribute("data-theme");
    applyTheme(cur === "dark" ? "light" : "dark", true);
  });
  $("#refresh-btn").addEventListener("click", () => { state.listSig = null; startListPolling(); });
  $("#settings-btn").addEventListener("click", openSettings);
  $("#settings-back").addEventListener("click", backToJobs);
  $("#back-btn").addEventListener("click", showEmpty);
  $("#search").addEventListener("input", (e) => { state.query = e.target.value; renderSidebar(); });
  $("#add-cluster").addEventListener("click", addCluster);
  $("#new-aliases").addEventListener("keydown", (e) => { if (e.key === "Enter") addCluster(); });
  document.querySelectorAll(".seg-btn").forEach((b) =>
    b.addEventListener("click", () => selectTab(b.dataset.tab)));
  $("#files-refresh").addEventListener("click", () => loadFiles(state.epoch));
  $("#download-btn").addEventListener("click", downloadOutputs);

  $("#route-btn").addEventListener("click", (e) => {
    if (toggleMenu($("#route-menu"), e)) {
      renderRouteMenu();
      loadHosts().then(renderRouteMenu);
    }
  });
  $("#more-btn").addEventListener("click", (e) => toggleMenu($("#more-menu"), e));
  document.querySelectorAll("#more-menu [data-copy]").forEach((b) => b.addEventListener("click", () => {
    const key = b.dataset.copy;
    const val = key === "logfile" ? (state.logfile || state.detail.logfile) : state.detail[key];
    copyText(val, b.textContent.replace(/^Copy /, ""));
    closeMenus();
  }));
  $("#cancel-btn").addEventListener("click", async () => {
    if (!state.detail) return;
    if (!confirm(`Cancel job ${state.detail.job_id} (${state.detail.name})?`)) return;
    try {
      const r = await post(`/api/jobs/${encodeURIComponent(state.detail.job_id)}/cancel`);
      toast(r.ok ? "Cancel requested" : "Cancel failed: " + (r.error || ""));
    } catch (e) {
      toast("Cancel failed: " + e.message);
    }
  });

  document.addEventListener("click", (e) => {
    if (!e.target.closest(".popmenu")) closeMenus();
  });
  document.addEventListener("keydown", (e) => {
    const typing = /^(INPUT|SELECT|TEXTAREA)$/.test(document.activeElement.tagName);
    if (e.key === "Escape") closeMenus();
    if (typing || e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.key === "ArrowDown" || e.key === "j") { e.preventDefault(); selectRelative(1); }
    if (e.key === "ArrowUp" || e.key === "k") { e.preventDefault(); selectRelative(-1); }
    if (e.key === "/") { e.preventDefault(); $("#search").focus(); }
  });

  const con = $("#console");
  con.addEventListener("scroll", () => {
    state.stick = con.scrollHeight - con.scrollTop - con.clientHeight < 30;
    if (state.stick) $("#jump-btn").classList.add("hidden");
  });
  $("#jump-btn").addEventListener("click", () => {
    con.scrollTop = con.scrollHeight;
    state.stick = true;
    $("#jump-btn").classList.add("hidden");
  });

  // Coming back to a backgrounded tab: catch up now instead of waiting out the
  // long hidden-tab delay.
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) return;
    startListPolling();
    if (state.view === "job" && state.logTimer) {
      stopLogPolling();
      logLoop();
    }
  });

  loadHosts();
  openFromUrl().then(startListPolling);
}

// If the script is served from cache the document can already be parsed by the
// time this runs, and a DOMContentLoaded listener would never fire.
if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}
