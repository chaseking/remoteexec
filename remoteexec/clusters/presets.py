"""The `axon` preset — §18. Conda-first."""
from __future__ import annotations

from ..environment import ApptainerEnv, CondaEnv
from .slurm import SlurmCluster


class AxonCluster(SlurmCluster):
    """Preset for the `axon` Slurm cluster (ssh alias `axon`). Conda-first."""

    def __init__(self, *, host="axon",conda_env: str = "base", pre: list[str] | None = None,
                 init: bool = True, local_conda_env: str | None = None, env=None,
                 project=None, local_dir=None, setup=(), **overrides):
        # axon's non-interactive sbatch shells don't define the `conda` shell
        # function, so `conda activate` needs the hook (§10.2). init defaults
        # True here because that's empirically required on this cluster.
        remote_env = env or CondaEnv(conda_env, pre=pre or [], init=init)
        local_env = CondaEnv(local_conda_env) if local_conda_env else None
        defaults = dict(time="04:00:00", cpus_per_task=8, mem="32G")
        super().__init__(
            host=host,
            remote_root="~/remoteexec",
            project=project,
            local_dir=local_dir,
            env=remote_env,
            local_env=local_env,
            setup=setup,
            default_resources=overrides.pop("default_resources", defaults),
            excludes=overrides.pop("excludes", ["data/raw/", "*.ckpt", "wandb/"]),
            **overrides,
        )


class AxonJaxGpuCluster(SlurmCluster):
    """Preset `axon-jax-gpu`: JAX GPU jobs on axon via the Apptainer JAX-CUDA container.

    Runs ``apptainer exec --nv /share/singularity/jax_cuda.sif python ...`` on partition
    ``gpu`` with ``--gres=gpu:1`` (§18). Unlike `AxonCluster` (conda-first), the function
    executes inside the container; your pip ``--user`` packages are forwarded in via
    ``APPTAINERENV_PYTHONPATH`` (see `ApptainerEnv`).

    Pass ``remote=True`` to target the off-site ``axon-remote`` ssh host instead of the
    on-site ``axon`` host (both ssh aliases live in your ``~/.ssh/config``).
    """

    SIF = "/share/singularity/jax_cuda.sif"
    HOST = "axon"
    HOST_REMOTE = "axon-remote"

    def __init__(self, *, remote: bool = False, sif: str | None = None, env=None,
                 project=None, local_dir=None, setup=(), **overrides):
        remote_env = env or ApptainerEnv(sif or self.SIF)
        defaults = dict(partition="gpu", gres="gpu:1", time="04:00:00",
                        cpus_per_task=8, mem="32G")
        host = overrides.pop("host", self.HOST_REMOTE if remote else self.HOST)
        super().__init__(
            host=host,
            remote_root="~/remoteexec",
            project=project,
            local_dir=local_dir,
            env=remote_env,
            setup=setup,
            default_resources=overrides.pop("default_resources", defaults),
            excludes=overrides.pop("excludes", ["data/raw/", "*.ckpt", "wandb/"]),
            **overrides,
        )
