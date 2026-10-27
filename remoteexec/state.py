"""Local state under ~/.remoteexec (override with $REMOTEEXEC_HOME).

``manifest.json`` maps job id -> where the job lives, so a restarted notebook,
the CLI and the GUI can all reattach to it. Small JSON caches (env parity,
code-sync signatures) live next to it.
"""
from __future__ import annotations

import fcntl
import json
import os
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path


def home() -> Path:
    return Path(os.environ.get("REMOTEEXEC_HOME") or "~/.remoteexec").expanduser()


def _manifest() -> Path:
    return home() / "manifest.json"


@contextmanager
def _locked(name: str = "manifest.lock"):
    """Serialize read-modify-write across processes.

    Concurrent submissions (a sweep driven by N processes, parallel agents)
    would otherwise lose entries when their load/save cycles interleave.
    """
    home().mkdir(parents=True, exist_ok=True)
    with open(home() / name, "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Per-process temp name: two processes must never race on one temp path.
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=1))
    os.replace(tmp, path)


def _load() -> dict:
    return read_json(_manifest())


def make_entry(job, *, env=None, local_dir=None, partition=None, **extra) -> dict:
    from .hosts import canonical

    entry = {
        "cluster_host": canonical(job.cluster.conn.host),
        "session": job.session,
        "workdir": job.workdir,
        "logdir": job.logdir,
        "name": job.name,
        "submitted_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "cwd": os.getcwd(),
    }
    for key in ("rundir", "outdir", "kind"):
        if getattr(job, key, None):
            entry[key] = getattr(job, key)
    if env is not None:
        entry["env"] = env
    if local_dir is not None:
        entry["local_dir"] = str(local_dir)
    if partition is not None:
        entry["partition"] = partition
    entry.update({k: v for k, v in extra.items() if v is not None})
    return entry


def record(job, **fields) -> None:
    record_many({str(job.job_id): make_entry(job, **fields)})


def record_many(entries: dict) -> None:
    with _locked():
        data = _load()
        data.update(entries)
        write_json(_manifest(), data)


def get(job_id: str) -> dict | None:
    return _load().get(str(job_id))


def all_jobs() -> dict:
    return _load()


def recent(*, under: str | Path | None = None, limit: int | None = None) -> list[tuple[str, dict]]:
    """Manifest entries newest first, optionally only those submitted from
    inside the directory tree ``under`` (e.g. the caller's cwd)."""
    items = list(_load().items())
    if under is not None:
        base = str(Path(under).resolve())

        def inside(p):
            return p == base or p.startswith(base.rstrip(os.sep) + os.sep)

        items = [(j, r) for j, r in items if inside(r.get("cwd") or r.get("local_dir") or "")]
    items.sort(key=lambda kv: kv[1].get("submitted_at", ""), reverse=True)
    return items[:limit] if limit else items


def prune(older_than_days: float, *, dry_run: bool = False) -> dict:
    """Drop manifest entries submitted more than N days ago; returns them."""
    cutoff = (datetime.now() - timedelta(days=older_than_days)).strftime("%Y-%m-%dT%H:%M:%S")
    with _locked():
        data = _load()
        old = {j: r for j, r in data.items() if r.get("submitted_at", "") <= cutoff}
        if old and not dry_run:
            write_json(_manifest(), {j: r for j, r in data.items() if j not in old})
    return old
