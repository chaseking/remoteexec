"""Environments — §10. CondaEnv is the primary/default strategy."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@runtime_checkable
class Environment(Protocol):
    def activation_script(self) -> str: ...

    def launcher(self) -> str: ...


def env_label(env) -> str:
    """Short human-readable label for an environment, for the manifest/GUI.
    e.g. CondaEnv("base") -> "conda:base", UvEnv -> "uv", VenvEnv -> "venv"."""
    if env is None:
        return ""
    cls = type(env).__name__
    if cls == "CondaEnv":
        return f"conda:{env.name}"
    if cls == "VenvEnv":
        return f"venv:{env.path.rstrip('/').rsplit('/', 1)[-1]}"
    if cls == "UvEnv":
        return "uv"
    if cls == "ModuleEnv":
        return "ml:" + ",".join(env.modules)
    if cls == "ApptainerEnv":
        return "apptainer:" + env.sif.rstrip("/").rsplit("/", 1)[-1]
    if cls == "ChainEnv":
        return " | ".join(env_label(p) for p in env.parts)
    if cls == "RawSetup":
        return "raw"
    return cls


@dataclass
class UvEnv:
    project_dir: str | None = None  # defaults to the synced workdir (cwd at run time)
    python: str | None = None  # e.g. "3.11"
    sync: bool = True
    frozen: bool = True
    with_packages: tuple[str, ...] = ("cloudpickle",)
    preamble: str = ""

    def activation_script(self) -> str:
        lines = [self.preamble] if self.preamble else []
        if self.sync:
            cmd = "uv sync --frozen" if self.frozen else "uv sync"
            lines.append(f"cd {self.project_dir} && {cmd}" if self.project_dir else cmd)
        return "\n".join(lines)

    def launcher(self) -> str:
        py = f"--python {self.python} " if self.python else ""
        wth = " ".join(f"--with {p}" for p in self.with_packages)
        return f"uv run {py}{wth} python".strip().replace("  ", " ")


@dataclass
class ModuleEnv:
    modules: str | list[str]

    def __post_init__(self):
        if isinstance(self.modules, str):
            self.modules = [self.modules]

    def activation_script(self) -> str:
        return "\n".join(f"module load {m}" for m in self.modules)

    def launcher(self) -> str:
        return "python"


@dataclass
class CondaEnv:
    """Primary environment for axon. Activation is simply ``conda activate <name>``,
    assuming the conda module is already loaded (conda on PATH)."""

    name: str
    pre: list[str] = field(default_factory=list)  # extra pre-run commands
    init: bool = False  # emit `eval "$(conda shell.bash hook)"` first

    def activation_script(self) -> str:
        lines = list(self.pre)
        if self.init:
            lines.append('eval "$(conda shell.bash hook)"')
        lines.append(f"conda activate {self.name}")
        return "\n".join(lines)

    def launcher(self) -> str:
        return "python"  # cloudpickle must be in the conda env


@dataclass
class ApptainerEnv:
    """Run python inside an Apptainer/Singularity container (e.g. axon's JAX-CUDA image).
    The runner is invoked as ``apptainer exec --nv <sif> python ...``.

    Host packages are forwarded into the container via ``APPTAINERENV_PYTHONPATH`` —
    Apptainer only passes through env vars prefixed ``APPTAINERENV_``, so this is the
    correct mechanism to expose a user's ``~/.local`` site-packages inside the image.
    """

    sif: str  # path to the .sif image, e.g. /share/singularity/jax_cuda.sif
    nv: bool = True  # --nv: expose the host GPU/CUDA driver to the container
    module_purge: bool = True  # `module purge` before exec (clean module env)
    # forwarded into the container so it sees the user's local pip --user packages
    pythonpath: str | None = "$HOME/.local/lib/python3.12/site-packages"
    pre: list[str] = field(default_factory=list)  # extra pre-run commands

    def activation_script(self) -> str:
        lines = list(self.pre)
        if self.module_purge:
            lines.append("module purge")
        if self.pythonpath:
            lines.append(f"export APPTAINERENV_PYTHONPATH={self.pythonpath}")
        return "\n".join(lines)

    def launcher(self) -> str:
        nv = "--nv " if self.nv else ""
        return f"apptainer exec {nv}{self.sif} python"


@dataclass
class VenvEnv:
    path: str

    def activation_script(self) -> str:
        return f"source {self.path}/bin/activate"

    def launcher(self) -> str:
        return "python"


@dataclass
class RawSetup:
    script: str = ""

    def activation_script(self) -> str:
        return self.script  # default = no-op activation

    def launcher(self) -> str:
        return "python"


@dataclass
class ChainEnv:  # e.g. module load THEN conda activate
    parts: list[Environment]

    def activation_script(self) -> str:
        return "\n".join(p.activation_script() for p in self.parts)

    def launcher(self) -> str:
        for p in reversed(self.parts):
            if hasattr(p, "launcher"):
                return p.launcher()
        return "python"
