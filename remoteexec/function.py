"""RemoteFunction + @cluster.function decorator — §6."""
from __future__ import annotations

import functools
import subprocess
import sys
import tempfile
from pathlib import Path

from .resources import SlurmResources
from .serialize import load_result, pack_payload

_RESERVED = {"_resources", "_name", "_env", "_setup", "_sync"}


class RemoteFunction:
    def __init__(self, cluster, fn, *, name=None, env=None, setup=None,
                 sync=True, resources=None):
        self.cluster = cluster
        self.fn = fn
        self.name = name or fn.__name__
        self.env = env
        self.setup = list(setup or [])
        self.sync = sync
        self.resources = resources or SlurmResources()
        functools.update_wrapper(self, fn)

    def _split_kwargs(self, kwargs):
        reserved = {k: kwargs.pop(k) for k in list(kwargs) if k in _RESERVED}
        return reserved, kwargs

    def __call__(self, *args, **kwargs):
        reserved, kwargs = self._split_kwargs(kwargs)
        per_call = SlurmResources(**reserved.get("_resources", {}))
        resources = self.resources.merge(per_call)
        return self.cluster.submit(
            self.fn, args, kwargs, resources,
            env=reserved.get("_env", self.env),
            name=reserved.get("_name", self.name),
            setup=[*self.setup, *reserved.get("_setup", [])],
            sync=reserved.get("_sync", self.sync),
        )

    def map(self, iterable, *, _resources=None, **kw):
        resources = self.resources.merge(SlurmResources(**(_resources or {})))
        return self.cluster.map(
            self.fn, iterable, resources=resources, env=self.env,
            name=self.name, setup=self.setup, **kw
        )

    def local(self, *args, **kwargs):
        """Run locally — §10.1. Uses cluster.local_env in a subprocess if set,
        else runs in-process for speed."""
        reserved, kwargs = self._split_kwargs(kwargs)
        env = reserved.get("_env", self.cluster.local_env)
        if env is None:
            return self.fn(*args, **kwargs)
        return _run_local_subprocess(self.fn, args, kwargs, env)


def _run_local_subprocess(fn, args, kwargs, env):
    from . import _runner

    runner_path = Path(_runner.__file__)
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        payload = d / "payload.pkl"
        result = d / "result.pkl"
        payload.write_bytes(pack_payload(fn, args, kwargs))
        launcher = env.launcher()
        activation = env.activation_script()
        script = f"{activation}\n{launcher} {runner_path} {payload} {result}\n"
        proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
        if not result.exists():
            sys.stderr.write(proc.stdout + proc.stderr)
            raise RuntimeError(f"local run failed (rc={proc.returncode})")
        return load_result(result)
