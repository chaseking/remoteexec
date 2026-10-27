"""remoteexec — edit code locally, execute on a remote Slurm cluster."""
from __future__ import annotations

import os

from .clusters.local import LocalCluster
from .clusters.presets import AxonCluster, AxonJaxGpuCluster
from .clusters.slurm import SlurmCluster
from .errors import (
    ConnectionError,
    EnvParityWarning,
    JobFailed,
    RemoteExecError,
    RemoteExecutionError,
)
from .job import Job, JobArray, JobState
from .resources import SlurmResources
from .serialize import __version__

__all__ = [
    "SlurmCluster", "LocalCluster", "AxonCluster", "AxonJaxGpuCluster",
    "SlurmResources", "Job", "JobArray", "JobState",
    "is_remote", "from_remote", "to_remote",
    "RemoteExecError", "RemoteExecutionError", "JobFailed", "ConnectionError",
    "EnvParityWarning",
    "__version__",
]


def is_remote() -> bool:
    """True inside a remotely-executing function; False for local runs (§10.1)."""
    return os.environ.get("REMOTEEXEC_REMOTE") == "1"


def from_remote(cluster, remote, local=None):
    return cluster.load_from_remote(remote, local)


def to_remote(cluster, local, remote):
    return cluster.save_to_remote(local, remote)
