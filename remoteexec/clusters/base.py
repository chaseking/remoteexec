"""Cluster ABC."""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from ..function import RemoteFunction
from ..resources import SlurmResources


class Cluster(ABC):
    local_env = None

    def function(self, fn=None, *, name=None, env=None, setup=None, run_local=False,
                 sync=None, **resource_kwargs):
        """Decorator: ``@cluster.function`` or ``@cluster.function(gpus=1, ...)``.

        ``run_local=True`` (or env ``REMOTEEXEC_LOCAL=1``) runs calls on this
        machine instead — handy for a quick smoke test.
        """
        resources = SlurmResources(**resource_kwargs)

        def deco(f):
            return RemoteFunction(self, f, name=name, env=env, setup=setup,
                                  sync=sync, resources=resources, run_local=run_local)

        return deco(fn) if fn is not None else deco

    @abstractmethod
    def submit(self, fn, args, kwargs, resources, *, env=None, name=None,
               setup=(), sync=None): ...

    @abstractmethod
    def map(self, fn, iterable, *, resources, **kw): ...

    @abstractmethod
    def run(self, command: str, *, name=None, env=None, setup=(), sync=None,
            **resources): ...

    @abstractmethod
    def status(self, job_id): ...

    @abstractmethod
    def cancel(self, job_id) -> None: ...

    @abstractmethod
    def job(self, job_id): ...

    @abstractmethod
    def make_path(self, *parts, mkdir=True): ...

    @abstractmethod
    def load_from_remote(self, remote, local=None) -> Path: ...

    @abstractmethod
    def save_to_remote(self, local, remote): ...
