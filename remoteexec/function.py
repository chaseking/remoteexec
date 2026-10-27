"""RemoteFunction — what ``@cluster.function`` returns."""
from __future__ import annotations

import functools
import os
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

from .job import JobArray, LocalJob, slug
from .resources import SlurmResources
from .serialize import load_result, pack_payload

_RESERVED = {"_resources", "_name", "_env", "_setup", "_sync"}


def force_local() -> bool:
    """``REMOTEEXEC_LOCAL=1`` runs every remote function locally instead."""
    return os.environ.get("REMOTEEXEC_LOCAL") == "1"


class RemoteFunction:
    def __init__(self, cluster, fn, *, name=None, env=None, setup=None,
                 sync=None, resources=None, run_local=False):
        self.cluster = cluster
        self.fn = fn
        self.name = name or fn.__name__
        self.env = env
        self.setup = list(setup or [])
        self.sync = sync  # None = the cluster's default
        self.resources = resources or SlurmResources()
        self.run_local = run_local
        functools.update_wrapper(self, fn)

    def __repr__(self):
        return f"<RemoteFunction {self.name}>"

    def _split_kwargs(self, kwargs):
        reserved = {k: kwargs.pop(k) for k in list(kwargs) if k in _RESERVED}
        return reserved, kwargs

    def __call__(self, *args, **kwargs):
        """Submit and return a Job immediately (``job.result()`` waits)."""
        if self.run_local or force_local():
            return self._local_job(args, kwargs)
        reserved, kwargs = self._split_kwargs(kwargs)
        per_call = SlurmResources(**reserved.get("_resources", {}))
        return self.cluster.submit(
            self.fn, args, kwargs, self.resources.merge(per_call),
            env=reserved.get("_env", self.env),
            name=reserved.get("_name", self.name),
            setup=[*self.setup, *reserved.get("_setup", [])],
            sync=reserved.get("_sync", self.sync),
        )

    def map(self, iterable, *, _resources=None, array_throttle=None, **kw):
        """One Slurm array job, one task per item (dict -> kwargs, tuple ->
        args, else a single arg). Returns a JobArray in input order."""
        res = dict(_resources or {})
        array_throttle = array_throttle or res.pop("array_throttle", None)
        if self.run_local or force_local():
            return JobArray([self._local_job(*_as_call(item)) for item in iterable])
        kw.setdefault("sync", self.sync)
        return self.cluster.map(
            self.fn, iterable, resources=self.resources.merge(SlurmResources(**res)),
            env=kw.pop("env", self.env), name=kw.pop("name", self.name),
            setup=self.setup, array_throttle=array_throttle, **kw,
        )

    def local(self, *args, **kwargs):
        """Run here and return the value. Uses ``cluster.local_env`` in a
        subprocess if set, else runs in-process. ``output_dir()`` maps to
        ``./remoteexec-out/<name>-local``."""
        reserved, kwargs = self._split_kwargs(kwargs)
        env = reserved.get("_env", self.cluster.local_env)
        outdir = Path.cwd() / "remoteexec-out" / f"{slug(self.name)}-local"
        if env is None:
            with _env_var("REMOTEEXEC_OUTPUT_DIR", str(outdir)):
                return self.fn(*args, **kwargs)
        return _run_local_subprocess(self.fn, args, kwargs, env, outdir=outdir)

    def _local_job(self, args, kwargs):
        value = self.local(*args, **kwargs)
        return LocalJob(value, self.name, Path.cwd() / "remoteexec-out" / f"{slug(self.name)}-local")


def _as_call(item):
    if isinstance(item, dict):
        return (), dict(item)
    if isinstance(item, tuple):
        return item, {}
    return (item,), {}


@contextmanager
def _env_var(key, value):
    old = os.environ.get(key)
    os.environ[key] = value
    try:
        yield
    finally:
        if old is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = old


def _run_local_subprocess(fn, args, kwargs, env, outdir=None):
    from . import _runner

    with tempfile.TemporaryDirectory() as d:
        payload, result = Path(d) / "payload.pkl", Path(d) / "result.pkl"
        payload.write_bytes(pack_payload(fn, args, kwargs))
        script = (f"{env.activation_script()}\n"
                  f"{env.launcher()} {Path(_runner.__file__)} {payload} {result}\n")
        run_env = dict(os.environ)
        if outdir:
            run_env["REMOTEEXEC_OUTPUT_DIR"] = str(outdir)
        proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=run_env)
        if not result.exists():
            sys.stderr.write(proc.stdout + proc.stderr)
            raise RuntimeError(f"local run failed (rc={proc.returncode})")
        return load_result(result)
