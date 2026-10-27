"""LocalCluster — subprocess execution for tests/offline use (§4)."""
from __future__ import annotations

from pathlib import Path

from ..function import _run_local_subprocess
from ..job import JobState
from .base import Cluster


class _ImmediateJob:
    """A job whose work already ran synchronously in-process."""

    def __init__(self, result, name):
        self._result = result
        self.name = name
        self.job_id = "local"

    def state(self):
        return JobState.COMPLETED

    def done(self):
        return True

    def wait(self, **kw):
        return JobState.COMPLETED

    def result(self, **kw):
        return self._result

    def stream(self, **kw):
        print("(local run — no streaming)")


class LocalCluster(Cluster):
    """Runs functions locally via the same pack->runner->load path (or in-process)."""

    def __init__(self, env=None, local_env=None):
        self.env = env
        self.local_env = local_env or env

    def submit(self, fn, args, kwargs, resources, *, env=None, name=None,
               setup=(), sync=True):
        use_env = env or self.local_env
        if use_env is None:
            result = fn(*args, **kwargs)
        else:
            result = _run_local_subprocess(fn, args, kwargs, use_env)
        return _ImmediateJob(result, name or getattr(fn, "__name__", "job"))

    def map(self, fn, iterable, *, resources, **kw):
        from ..job import JobArray
        jobs = []
        for item in iterable:
            if isinstance(item, dict):
                jobs.append(self.submit(fn, (), item, resources, **kw))
            elif isinstance(item, tuple):
                jobs.append(self.submit(fn, item, {}, resources, **kw))
            else:
                jobs.append(self.submit(fn, (item,), {}, resources, **kw))
        return JobArray(jobs)

    def status(self, job_id):
        return JobState.COMPLETED

    def cancel(self, job_id):
        pass

    def job(self, job_id):
        raise KeyError("LocalCluster does not persist jobs")

    def make_path(self, *parts, mkdir=True) -> Path:
        p = Path(*[str(x) for x in parts]).expanduser()
        if mkdir:
            p.mkdir(parents=True, exist_ok=True)
        return p

    def load_from_remote(self, remote, local=None) -> Path:
        return Path(remote).expanduser()

    def save_to_remote(self, local, remote) -> Path:
        return Path(remote).expanduser()
