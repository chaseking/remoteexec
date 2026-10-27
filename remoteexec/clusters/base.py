"""Cluster ABC — §6."""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable

from ..function import RemoteFunction
from ..resources import SlurmResources


class Cluster(ABC):
    local_env = None

    def function(self, *, name=None, env=None, setup=None, run_local=False,
                 sync=True, **resource_kwargs) -> Callable[[Callable], RemoteFunction]:
        resources = SlurmResources(**resource_kwargs)

        def deco(fn):
            return RemoteFunction(
                self, fn, name=name, env=env, setup=setup,
                sync=sync, resources=resources,
            )

        return deco

    @abstractmethod
    def submit(self, fn, args, kwargs, resources, *, env=None, name=None,
               setup=(), sync=True): ...

    @abstractmethod
    def map(self, fn, iterable, *, resources, **kw): ...

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
