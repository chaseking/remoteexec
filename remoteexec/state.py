"""Local manifest — §20. ~/.remoteexec/manifest.json."""
from __future__ import annotations

import fcntl
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path

MANIFEST = Path("~/.remoteexec/manifest.json").expanduser()
LOCK = MANIFEST.parent / "manifest.lock"


@contextmanager
def _locked():
    """Serialize the manifest read-modify-write across processes.

    Concurrent submissions (a sweep driven by N processes, or several evaluators
    submitting at once) otherwise interleave _load()/_save() two ways, both bad:
    entries are silently lost, and -- because every process writes the *same*
    ``manifest.json.tmp`` -- one process's os.replace() can move the temp file out
    from under another, which fails the whole submission with FileNotFoundError.
    """
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK, "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _load() -> dict:
    if not MANIFEST.exists():
        return {}
    try:
        return json.loads(MANIFEST.read_text())
    except Exception:
        return {}


def _save(data: dict) -> None:
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    # Per-process temp name: two processes must never race on the same temp path.
    tmp = MANIFEST.with_name(f"{MANIFEST.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, MANIFEST)  # atomic


def record(job, *, env=None, local_dir=None, partition=None) -> None:
    entry = {
        "cluster_host": job.cluster.conn.host,
        "session": job.session,
        "workdir": job.workdir,
        "logdir": job.logdir,
        "name": job.name,
        "submitted_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    if getattr(job, "rundir", None):
        entry["rundir"] = job.rundir
    # Optional, GUI-facing fields. Kept optional so older manifest entries
    # (and callers that don't pass them) still load fine.
    if env is not None:
        entry["env"] = env
    if local_dir is not None:
        entry["local_dir"] = str(local_dir)
    if partition is not None:
        entry["partition"] = partition
    # Load-modify-save under the lock: reading outside it would let a concurrent
    # submission's entry be read-then-overwritten.
    with _locked():
        data = _load()
        data[str(job.job_id)] = entry
        _save(data)


def get(job_id: str) -> dict | None:
    return _load().get(str(job_id))


def all_jobs() -> dict:
    return _load()
