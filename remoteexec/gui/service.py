"""Backend service for the job-monitor GUI.

Aggregates the local manifest with live Slurm status (batched per host), serves
incremental logs, and cancels jobs. Pure helpers (formatting + filtering) are
kept module-level so they can be unit-tested without a cluster.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta

from .. import state
from ..clusters.slurm import SlurmCluster, batch_status
from ..logtail import MAX_CHUNK, log_filename, read_chunk, resolve_raw_id

# Every SSH call the GUI makes is bounded: an unbounded one parks a worker
# thread forever if a login node hangs, and enough parked workers take the whole
# UI down with them (the page itself stops loading).
SSH_TIMEOUT = 25  # seconds

# How much history the first read of a log pulls in. Opening a job that has been
# printing for two days should cost one small read, not a whole-file transfer.
INITIAL_TAIL = 256 * 1024

# Slurm states that mean "not finished yet".
RUNNING_STATES = {
    "PENDING", "RUNNING", "CONFIGURING", "REQUEUED", "RESIZING", "SUSPENDED",
    "COMPLETING",
}

FILTER_WINDOWS = {  # seconds
    "finished_6h": 6 * 3600,
    "finished_1d": 24 * 3600,
    "finished_7d": 7 * 24 * 3600,
}

DASH = "—"

# Above this many jobs, ask accounting for a time window of the user's jobs
# instead of naming every id: `sacct -j` slows down markedly as the id list
# grows (a 700-job manifest measured ~13s per sweep, versus ~1s windowed).
BY_ID_MAX = 50
# Window for that scan. It only has to cover the widest "recently finished"
# filter; anything still running is picked up from the queue regardless of age.
WINDOW_MARGIN = 24 * 3600
NO_FILTER_WINDOW = 30 * 24 * 3600


# --------------------------------------------------------------------------- #
# Pure helpers (unit-tested)
# --------------------------------------------------------------------------- #
def is_running(state_str: str) -> bool:
    return (state_str or "").upper() in RUNNING_STATES


def format_elapsed(raw: str) -> str:
    """Normalize a Slurm elapsed string to ``[Nd ][HH:]MM:SS``.

    Accepts ``D-HH:MM:SS``, ``HH:MM:SS`` or ``MM:SS``. Drops a leading ``00:``
    hours group; keeps days as a ``Nd`` prefix.
    """
    raw = (raw or "").strip()
    if not raw or raw in ("Unknown", "INVALID"):
        return DASH
    days = 0
    if "-" in raw:
        d, raw = raw.split("-", 1)
        try:
            days = int(d)
        except ValueError:
            days = 0
    parts = raw.split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return raw
    if len(nums) == 3:
        hh, mm, ss = nums
    elif len(nums) == 2:
        hh, mm, ss = 0, nums[0], nums[1]
    else:
        hh, mm, ss = 0, 0, nums[0]
    core = f"{hh:02d}:{mm:02d}:{ss:02d}" if hh else f"{mm:02d}:{ss:02d}"
    return f"{days}d {core}" if days else core


def parse_slurm_time(raw: str):
    """Parse a Slurm timestamp (``2026-06-18T08:37:52``) to datetime, or None."""
    raw = (raw or "").strip()
    if not raw or raw in ("Unknown", "None", "N/A"):
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def format_started(sacct_start: str, submitted_at: str) -> str:
    dt = parse_slurm_time(sacct_start) or parse_slurm_time(submitted_at)
    return dt.strftime("%Y-%m-%d %H:%M") if dt else DASH


def started_iso(sacct_start: str, submitted_at: str) -> str:
    """Raw ISO-8601 start time (sacct Start preferred, else submit time) for the
    client to render relative to the viewer's local time; "" when unknown."""
    dt = parse_slurm_time(sacct_start) or parse_slurm_time(submitted_at)
    return dt.isoformat() if dt else ""


def window_start(filters, now=None):
    """Oldest time the selected filters can possibly need, as a datetime."""
    now = now or datetime.now()
    if not filters:
        span = NO_FILTER_WINDOW
    else:
        span = max(
            [FILTER_WINDOWS[k] for k in filters if k in FILTER_WINDOWS] or [0]
        )
    return now - timedelta(seconds=span + WINDOW_MARGIN)


def matches_filters(state_str, end_dt, filters, now=None) -> bool:
    """Union of the selected filter buckets."""
    if not filters:
        return True
    now = now or datetime.now()
    if "running" in filters and is_running(state_str):
        return True
    if not is_running(state_str) and end_dt is not None:
        age = (now - end_dt).total_seconds()
        for key, window in FILTER_WINDOWS.items():
            if key in filters and 0 <= age <= window:
                return True
    return False


def build_row(job_id, rec, live) -> dict:
    """Merge a manifest entry (``rec``) with live Slurm info (``live``)."""
    live = live or {}
    state_str = (live.get("state") or "").upper() or "UNKNOWN"
    workdir = rec.get("workdir", "")
    logfile = log_filename(rec.get("logdir", ""), job_id, live.get("raw", ""))
    return {
        "job_id": job_id,
        "name": rec.get("name", DASH),
        "status": state_str,
        "host": rec.get("cluster_host", DASH),
        "partition": rec.get("partition") or DASH,
        "project_path": rec.get("local_dir") or workdir or DASH,
        "env": rec.get("env") or DASH,
        "started": format_started(live.get("start", ""), rec.get("submitted_at", "")),
        "started_iso": started_iso(live.get("start", ""), rec.get("submitted_at", "")),
        "running": format_elapsed(live.get("elapsed", "")),
        "exitcode": live.get("exitcode") or DASH,
        "workdir": workdir,
        "logfile": logfile,
        "end": live.get("end", ""),
        "submitted_at": rec.get("submitted_at", ""),
    }


# --------------------------------------------------------------------------- #
# Service
# --------------------------------------------------------------------------- #
class JobService:
    # The TTL has to comfortably exceed the client's poll interval, or the table
    # poll and the log poll each miss the cache and every tick costs two full
    # sacct sweeps instead of sharing one.
    def __init__(self, cache_ttl: float = 6.0):
        self._clusters: dict[str, SlurmCluster] = {}
        self._cache_ttl = cache_ttl
        self._status_cache: dict[tuple[str, str], tuple[float, dict]] = {}
        self._lock = threading.Lock()
        self._cluster_lock = threading.Lock()
        self._host_locks: dict[str, threading.Lock] = {}
        self._raw_ids: dict[str, str] = {}

    def _cluster(self, host: str) -> SlurmCluster:
        # Requests are served from a thread pool, so two of them can race here;
        # without the lock both build a cluster and pay for its connect().
        with self._cluster_lock:
            c = self._clusters.get(host)
            if c is None:
                c = SlurmCluster(host=host, check_env=False)
                c.connect()
                self._clusters[host] = c
            return c

    def _host_lock(self, host: str) -> threading.Lock:
        with self._lock:
            lk = self._host_locks.get(host)
            if lk is None:
                lk = self._host_locks[host] = threading.Lock()
            return lk

    def _take_cached(self, host: str, ids):
        """Split *ids* into {id: info} already cached and the ids still stale."""
        now = time.time()
        fresh, stale = {}, []
        with self._lock:
            for jid in ids:
                got = self._status_cache.get((host, jid))
                if got and now - got[0] < self._cache_ttl:
                    fresh[jid] = got[1]
                else:
                    stale.append(jid)
        return fresh, stale

    def _statuses(self, host: str, ids, *, since=None) -> dict:
        """Status for *ids* on *host*, querying only what the cache lacks.

        Cached per job rather than per host, so opening one job's console costs
        a single-id query (fast whatever the manifest size) instead of waiting
        on a sweep of every job ever submitted.
        """
        ids = [str(j) for j in ids]
        fresh, stale = self._take_cached(host, ids)
        if not stale:
            return fresh
        # One refresh per host at a time: concurrent pollers that all miss the
        # cache would otherwise fire the same sweep in parallel.
        with self._host_lock(host):
            more_fresh, stale = self._take_cached(host, stale)  # filled while waiting?
            fresh.update(more_fresh)
            if not stale:
                return fresh
            try:
                info = batch_status(
                    self._cluster(host).conn, stale, timeout=SSH_TIMEOUT,
                    since=since if len(stale) > BY_ID_MAX else None,
                )
            except Exception:
                info = {}
            now = time.time()
            with self._lock:
                for jid in stale:  # cache misses too, so they aren't re-asked
                    self._status_cache[(host, jid)] = (now, info.get(jid, {}))
            fresh.update({j: info.get(j, {}) for j in stale})
            return fresh

    def list_jobs(self, filters=None) -> list[dict]:
        manifest = state.all_jobs()
        # group ids by host
        by_host: dict[str, list[str]] = {}
        for jid, rec in manifest.items():
            by_host.setdefault(rec.get("cluster_host", ""), []).append(jid)

        rows = []
        now = datetime.now()
        since = window_start(filters, now)
        for host, ids in by_host.items():
            if not host:
                continue
            live = self._statuses(host, ids, since=since)
            for jid in ids:
                rec = manifest[jid]
                row = build_row(jid, rec, live.get(jid))
                end_dt = parse_slurm_time(row["end"])
                if matches_filters(row["status"], end_dt, filters, now):
                    rows.append(row)
        # newest first by submitted time
        rows.sort(key=lambda r: r.get("submitted_at", ""), reverse=True)
        return rows

    def detail(self, job_id: str) -> dict | None:
        rec = state.get(job_id)
        if rec is None:
            return None
        live = self._statuses(rec.get("cluster_host", ""), [job_id])
        return build_row(job_id, rec, live.get(str(job_id)))

    def read_log(self, job_id: str, offset: int = 0) -> dict:
        """One incremental read of a job's log, plus its current Slurm state.

        ``offset`` is a byte position in the remote file and is echoed back as
        ``next_offset``; the client sends it straight back on the next poll.
        Failures are reported in the payload rather than raised, so a flaky
        login node leaves the viewer with a message instead of a dead poll loop.
        """
        offset = max(0, int(offset))
        rec = state.get(job_id)
        if rec is None:
            return {"text": "", "next_offset": offset, "state": "UNKNOWN",
                    "exists": False, "error": "unknown job"}
        host = rec.get("cluster_host", "")
        # Status first: it carries the allocation number that names an array
        # task's log file. Scoped to this one job, so the console does not wait
        # on a sweep of the whole manifest.
        live = self._statuses(host, [job_id]).get(str(job_id), {})
        st = (live.get("state") or "UNKNOWN").upper()
        try:
            conn = self._cluster(host).conn
            logfile = log_filename(
                rec.get("logdir", ""), job_id, self._raw_id(conn, job_id, live)
            )
            chunk = read_chunk(
                conn, logfile, offset,
                timeout=SSH_TIMEOUT, max_bytes=MAX_CHUNK,
                initial_tail=INITIAL_TAIL,
            )
        except Exception as e:
            return {"text": "", "next_offset": offset, "state": st,
                    "exists": False, "error": str(e)}
        return {
            "text": chunk.text,
            "next_offset": chunk.next_offset,
            "state": st,
            "exists": chunk.exists,
            "stalled": chunk.stalled,
            "skipped": chunk.skipped,
            "more": chunk.more,
            "logfile": logfile,
        }

    def _raw_id(self, conn, job_id: str, live: dict) -> str:
        """Allocation number for *job_id*, cached for the life of the service.

        Usually free — ``batch_status`` already reports it. Only an array task
        that accounting has no row for costs an extra lookup, and the answer
        never changes, so it is remembered.
        """
        jid = str(job_id)
        if "_" not in jid:
            return jid
        raw = (live or {}).get("raw") or ""
        if raw:
            return raw
        with self._lock:
            cached = self._raw_ids.get(jid)
        if cached:
            return cached
        raw = resolve_raw_id(conn, jid, timeout=SSH_TIMEOUT)
        with self._lock:
            self._raw_ids[jid] = raw
        return raw

    def cancel(self, job_id: str) -> dict:
        rec = state.get(job_id)
        if rec is None:
            return {"ok": False, "error": "unknown job"}
        try:
            self._cluster(rec.get("cluster_host", "")).cancel(job_id)
        except Exception as e:
            return {"ok": False, "error": str(e)}
        with self._lock:  # bust cache so the new state shows promptly
            self._status_cache.pop((rec.get("cluster_host", ""), str(job_id)), None)
        return {"ok": True}
