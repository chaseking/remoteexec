"""Presets for the `axon` cluster."""
from __future__ import annotations

from ..environment import ApptainerEnv, CondaEnv
from .slurm import SlurmCluster

_EXCLUDES = ["data/raw/", "*.ckpt", "wandb/"]


class AxonCluster(SlurmCluster):
    """`axon` (ssh alias), conda-first. axon has no default partition, so
    pass one per function/call (e.g. ``partition="ctn"``)."""

    def __init__(self, *, host="axon", conda_env: str = "base", pre: list[str] | None = None,
                 init: bool = True, local_conda_env: str | None = None, env=None, **overrides):
        # axon's non-interactive sbatch shells don't define the `conda` shell
        # function, so `conda activate` needs the hook: init defaults True.
        overrides.setdefault("default_resources", dict(time="04:00:00", cpus_per_task=8, mem="32G"))
        overrides.setdefault("excludes", list(_EXCLUDES))
        super().__init__(
            host=host,
            env=env or CondaEnv(conda_env, pre=pre or [], init=init),
            local_env=overrides.pop("local_env", None) or (
                CondaEnv(local_conda_env) if local_conda_env else None),
            **overrides,
        )


class AxonJaxGpuCluster(SlurmCluster):
    """JAX GPU jobs on axon inside the Apptainer JAX-CUDA image, partition
    ``gpu`` with one GPU. ``remote=True`` dials the off-site ``axon-remote``
    alias (or register both aliases once: ``remoteexec host add axon axon-remote``)."""

    SIF = "/share/singularity/jax_cuda.sif"
    HOST = "axon"
    HOST_REMOTE = "axon-remote"

    def __init__(self, *, remote: bool = False, sif: str | None = None, env=None, **overrides):
        overrides.setdefault("default_resources", dict(
            partition="gpu", gres="gpu:1", time="04:00:00", cpus_per_task=8, mem="32G"))
        overrides.setdefault("excludes", list(_EXCLUDES))
        host = overrides.pop("host", self.HOST_REMOTE if remote else self.HOST)
        super().__init__(host=host, env=env or ApptainerEnv(sif or self.SIF), **overrides)


PRESETS = {"axon": AxonCluster, "axon-jax-gpu": AxonJaxGpuCluster}
