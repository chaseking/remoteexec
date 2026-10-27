"""remoteexec — edit code locally, run it on a remote Slurm cluster."""
from __future__ import annotations

from .clusters.local import LocalCluster
from .clusters.presets import AxonCluster, AxonJaxGpuCluster
from .clusters.slurm import SlurmCluster
from .config import resolve_cluster
from .errors import (
    ConnectionError,
    EnvParityWarning,
    JobFailed,
    JobTimeout,
    RemoteExecError,
    RemoteExecutionError,
)
from .job import Job, JobArray, JobState
from .resources import SlurmResources
from .runtime import is_remote, output_dir
from .serialize import __version__

__all__ = [
    "SlurmCluster", "LocalCluster", "AxonCluster", "AxonJaxGpuCluster", "resolve_cluster",
    "SlurmResources", "Job", "JobArray", "JobState",
    "is_remote", "output_dir", "from_remote", "to_remote",
    "RemoteExecError", "RemoteExecutionError", "JobFailed", "JobTimeout", "ConnectionError",
    "EnvParityWarning",
    "__version__",
]


def from_remote(cluster, remote, local=None):
    return cluster.load_from_remote(remote, local)


def to_remote(cluster, local, remote):
    return cluster.save_to_remote(local, remote)
