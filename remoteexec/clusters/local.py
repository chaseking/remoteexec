"""LocalCluster — runs everything on this machine (tests, offline work)."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from ..function import _as_call, _env_var, _run_local_subprocess
from ..job import JobArray, JobState, LocalJob, slug
from .base import Cluster


class LocalCluster(Cluster):
    """Same API as SlurmCluster; work runs synchronously (in-process, or in
    a subprocess under ``env`` if given)."""

    def __init__(self, env=None, local_env=None):
        self.env = env
        self.local_env = local_env or env

    def _outdir(self, name) -> Path:
        return Path.cwd() / "remoteexec-out" / f"{slug(name)}-local"

    def submit(self, fn, args, kwargs, resources, *, env=None, name=None,
               setup=(), sync=None):
        name = name or getattr(fn, "__name__", "job")
        use_env, outdir = env or self.local_env, self._outdir(name)
        if use_env is None:
            with _env_var("REMOTEEXEC_OUTPUT_DIR", str(outdir)):
                result = fn(*args, **kwargs)
        else:
            result = _run_local_subprocess(fn, args, kwargs, use_env, outdir=outdir)
        return LocalJob(result, name, outdir)

    def map(self, fn, iterable, *, resources, **kw):
        kw.pop("array_throttle", None)
        return JobArray([self.submit(fn, *_as_call(item), resources, **kw) for item in iterable])

    def run(self, command, *, name=None, env=None, setup=(), sync=None, **resources):
        name = name or command.split()[0]
        outdir = self._outdir(name)
        outdir.mkdir(parents=True, exist_ok=True)
        script = "\n".join([*setup, command])
        rc = subprocess.run(["bash", "-c", script],
                            env={**os.environ, "REMOTEEXEC_OUTPUT_DIR": str(outdir)}).returncode
        return LocalJob(rc, name, outdir)

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
