"use strict";

const POLL_MS = 3000;
const CATCHUP_MS = 120;       // server still has buffered bytes: come back fast
const ERROR_MS = 6000;        // back off a little after a failed poll
const HIDDEN_MS = 15000;      // background tab: keep polling, just rarely
const REQUEST_TIMEOUT_MS = 30000;
const MAX_CONSOLE_CHARS = 1000000;  // keep the console node from growing forever
const FLUSH_READS = 3;        // extra reads after a job goes terminal
const DEFAULT_FILTERS = ["running", "finished_1d"];

const state = {
  filters: new Set(DEFAULT_FILTERS),
  jobs: [],
  tableTimer: null,
  tableBusy: false,
  tableSig: null,
  detail: null,    // current job row
  // `epoch` is bumped every time the console switches jobs or closes. Every log
  // response carries the epoch it was issued under and is dropped if it no
  // longer matches, so a reply that arrives after the user navigated away can
  // never be appended to whatever is on screen now.
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
};

// ---------- theme ----------
function applyTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme);
  try { localStorage.setItem("rxtheme", theme); } catch (e) {}
}
function initTheme() {
  let theme = "light";
  try { theme = localStorage.getItem("rxtheme") || "light"; } catch (e) {}
  applyTheme(theme);
}
function toggleTheme() {
  const cur = document.documentElement.getAttribute("data-theme");
  applyTheme(cur === "dark" ? "light" : "dark");
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

function pillClass(status) {
  const s = (status || "").toUpperCase();
  if (["RUNNING", "COMPLETING", "RESIZING"].includes(s)) return "running";
  if (["PENDING", "CONFIGURING", "REQUEUED", "SUSPENDED"].includes(s)) return "pending";
  if (s === "COMPLETED") return "completed";
  if (["FAILED", "NODE_FAIL", "OUT_OF_MEMORY", "BOOT_FAIL"].includes(s)) return "failed";
  if (["CANCELLED", "TIMEOUT"].includes(s)) return "cancelled";
  return "unknown";
}

function makePill(status) {
  const p = el("span", "pill " + pillClass(status));
  p.textContent = status || "UNKNOWN";
  return p;
}

function formatBytes(n) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${Math.round(n / 1024)} KB`;
  return `${(n / 1048576).toFixed(1)} MB`;
}

// Pretty, locale-relative start time: "Today, 8:30am" / "Yesterday, 9:15pm" /
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
  const today = new Date();
  const startOfDay = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate());
  const days = Math.round((startOfDay(today) - startOfDay(d)) / 86400000);
  if (days === 0) return `${time}`;
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

function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.remove("show"), 1800);
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
  if (!state.jobs.length) {
    $("#empty").classList.remove("hidden");
    return;
  }
  $("#empty").classList.add("hidden");
  const frag = document.createDocumentFragment();
  for (const job of state.jobs) {
    const tr = el("tr");
    const tdStatus = el("td");
    tdStatus.appendChild(makePill(job.status));
    tr.appendChild(tdStatus);
    tr.appendChild(el("td", "col-name", job.name));
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
  if (!msg) {
    n.classList.add("hidden");
    n.textContent = "";
    return;
  }
  n.textContent = msg;
  n.classList.remove("hidden");
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
  const c = $("#console");
  c.replaceChildren();
  state.gotLog = false;
  state.logChars = 0;
  state.trimmed = false;
  state.stalled = false;
  state.skipped = 0;
  setNote("");
  setPlaceholder("Loading…");
}

// ---------- detail / console view ----------
function openDetail(job) {
  stopLogPolling();          // a second click must not leave a poller behind
  state.epoch += 1;
  state.detail = job;
  state.logOffset = 0;
  state.flushLeft = FLUSH_READS;
  state.stick = true;
  state.logfile = job.logfile || "";
  resetConsole();
  $("#detail-name").textContent = job.name;
  const st = $("#detail-status");
  st.className = "pill " + pillClass(job.status);
  st.textContent = job.status;
  $("#detail-meta").innerHTML =
    `<span><b>job</b> ${job.job_id}</span>` +
    `<span><b>host</b> ${job.host}</span>` +
    `<span><b>partition</b> ${job.partition}</span>` +
    `<span><b>env</b> ${job.env}</span>` +
    `<span><b>started</b> ${formatStarted(job.started_iso)}</span>` +
    `<span><b>elapsed</b> ${job.running}</span>`;
  $("#view-main").classList.add("hidden");
  $("#view-detail").classList.remove("hidden");
  stopTablePolling();
  logLoop();
}

function closeDetail() {
  stopLogPolling();
  state.epoch += 1;          // discard anything still in flight
  state.detail = null;
  $("#menu-list").classList.add("hidden");
  $("#view-detail").classList.add("hidden");
  $("#view-main").classList.remove("hidden");
  startTablePolling();
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
  const pill = $("#detail-status");
  pill.className = "pill " + pillClass(st);
  pill.textContent = st;
  if (state.detail) state.detail.status = st;

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
    if (state.flushLeft > 0 && data.text) state.flushLeft = FLUSH_READS;
    if (state.flushLeft-- > 0) return POLL_MS;
    if (!state.gotLog) {
      setPlaceholder(
        "(no log output found — the remote session directory may have " +
        "been removed, or the job produced no output)");
    }
    return null;
  }
  return POLL_MS;
}

// Self-scheduling loop: the next read is only queued once the previous one has
// finished. A fixed setInterval instead lets slow polls overlap, and two reads
// issued at the same offset both return the same bytes — the duplicated lines.
// A hidden tab slows down rather than stopping: some embeddings report hidden
// while perfectly visible, and a stalled console is the bug we are fixing.
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

// ---------- polling control ----------
async function tableLoop() {
  state.tableTimer = null;
  if (state.detail) return;              // the console view owns polling
  await loadJobs();
  if (!state.detail) scheduleTable(document.hidden ? HIDDEN_MS : POLL_MS);
}
// Always cancel before arming: a manual refresh landing while a poll is in
// flight would otherwise leave two chained loops running, doubling the request
// rate with every click.
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
function init() {
  initTheme();
  document.querySelectorAll(".theme-toggle").forEach((b) =>
    b.addEventListener("click", (e) => { e.stopPropagation(); toggleTheme(); }));

  // filter dropdown
  const menu = $("#filter-menu");
  menu.querySelectorAll("input").forEach((cb) => {
    cb.checked = state.filters.has(cb.value);
    cb.addEventListener("change", () => {
      if (cb.checked) state.filters.add(cb.value);
      else state.filters.delete(cb.value);
      updateFilterChip();
      startTablePolling();   // refresh now, and restart the cycle from here
    });
  });
  updateFilterChip();

  $("#filter-btn").addEventListener("click", (e) => {
    e.stopPropagation();
    menu.classList.toggle("hidden");
  });
  $("#refresh-btn").addEventListener("click", () => startTablePolling());
  $("#back-btn").addEventListener("click", closeDetail);

  // actions menu
  const mlist = $("#menu-list");
  $("#menu-btn").addEventListener("click", (e) => {
    e.stopPropagation();
    mlist.classList.toggle("hidden");
  });
  const copy = (val, label) => {
    navigator.clipboard.writeText(val || "").then(() => toast(label + " copied"));
    mlist.classList.add("hidden");
  };
  $("#copy-workdir").addEventListener("click", () =>
    copy(state.detail && state.detail.workdir, "Remote workdir"));
  $("#copy-logfile").addEventListener("click", () =>
    copy(state.logfile || (state.detail && state.detail.logfile), "Log path"));
  $("#copy-project").addEventListener("click", () =>
    copy(state.detail && state.detail.project_path, "Project path"));
  $("#cancel-job").addEventListener("click", async () => {
    mlist.classList.add("hidden");
    if (!state.detail) return;
    if (!confirm(`Cancel job ${state.detail.job_id} (${state.detail.name})?`)) return;
    try {
      const r = await api(`/api/jobs/${state.detail.job_id}/cancel`, { method: "POST" });
      toast(r.ok ? "Cancel requested" : "Cancel failed: " + (r.error || ""));
    } catch (e) {
      toast("Cancel failed: " + e.message);
    }
  });

  // close popovers on outside click
  document.addEventListener("click", () => {
    menu.classList.add("hidden");
    mlist.classList.add("hidden");
  });

  // console scroll: detect whether user is stuck to bottom
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
    } else {
      startTablePolling();
    }
  });

  startTablePolling();
}

// If the script is served from cache the document can already be parsed by the
// time this runs, and a DOMContentLoaded listener would never fire — leaving a
// page that renders but never loads anything.
if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}
