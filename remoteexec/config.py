"""TOML config and cluster lookup by name.

Sources, highest priority first (clusters from different files are merged):
``<project root>/remoteexec.toml``, ``~/.config/remoteexec/config.toml``,
``[tool.remoteexec]`` in ``<project root>/pyproject.toml``::

    default = "axon"

    [clusters.axon]
    preset = "axon"              # optional: start from a preset
    sync = "cwd"                 # see remoteexec.sync
    setup = ["export WANDB_MODE=offline"]
    [clusters.axon.env]
    kind = "conda"
    name = "torch24"
    init = true
    [clusters.axon.default_resources]
    partition = "ctn"
    time = "04:00:00"
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from .environment import ApptainerEnv, CondaEnv, ModuleEnv, RawSetup, UvEnv, VenvEnv

_KEYS = ("remote_root", "project", "local_dir", "default_resources", "setup", "excludes",
         "user", "port", "identity_file", "respect_gitignore", "check_env", "sync")


def _search_paths() -> list[Path]:
    from .clusters.slurm import find_project_root  # lazy: avoid import cycle

    root = find_project_root(Path.cwd())
    return [
        root / "remoteexec.toml",
        Path("~/.config/remoteexec/config.toml").expanduser(),
        root / "pyproject.toml",  # [tool.remoteexec]
    ]


def _read(p: Path) -> dict | None:
    # imported here: the package also loads on the cluster, where tomli may be absent
    if sys.version_info >= (3, 11):
        import tomllib
    else:  # pragma: no cover
        import tomli as tomllib
    data = tomllib.loads(p.read_text())
    if p.name == "pyproject.toml":
        data = data.get("tool", {}).get("remoteexec")
    return data or None


def _find_config() -> dict | None:
    """Merge every source; a name defined in a higher-priority file wins."""
    merged: dict = {}
    clusters: dict = {}
    found = False
    for p in _search_paths():
        if not p.exists():
            continue
        data = _read(p)
        if not data:
            continue
        found = True
        if "default" in data:
            merged.setdefault("default", data["default"])
        for name, spec in data.get("clusters", {}).items():
            clusters.setdefault(name, spec)
    if not found:
        return None
    merged["clusters"] = clusters
    return merged


def _build_env(spec: dict | None):
    if not spec:
        return None
    kind = spec.get("kind", "conda")
    rest = {k: v for k, v in spec.items() if k != "kind"}
    if kind == "conda":
        return CondaEnv(spec["name"], pre=spec.get("pre", []), init=spec.get("init", False))
    if kind == "uv":
        return UvEnv(**rest)
    if kind == "venv":
        return VenvEnv(spec["path"])
    if kind == "module":
        return ModuleEnv(spec["modules"])
    if kind == "raw":
        return RawSetup(spec.get("script", ""))
    if kind == "apptainer":
        return ApptainerEnv(**rest)
    raise ValueError(f"unknown env kind: {kind}")


def load_cluster(cls, name=None, **overrides):
    cfg = _find_config()
    if cfg is None:
        raise FileNotFoundError("no remoteexec config found")
    name = name or cfg.get("default")
    clusters = cfg.get("clusters", {})
    if name is None:
        if len(clusters) != 1:
            raise ValueError(f"specify a cluster name (no default set); available: {sorted(clusters)}")
        name = next(iter(clusters))
    if name not in clusters:
        raise KeyError(f"cluster {name!r} not found in config; available: {sorted(clusters)}")
    c = clusters[name]
    kwargs = {k: c[k] for k in _KEYS if k in c}
    for k in ("env", "local_env"):
        if c.get(k):
            kwargs[k] = _build_env(c[k])
    kwargs.update(overrides)
    if c.get("preset"):
        from .clusters.presets import PRESETS

        if "host" in c:
            kwargs["host"] = c["host"]
        return PRESETS[c["preset"]](**kwargs)
    return cls(host=c.get("host", name), **kwargs)


def resolve_cluster(name: str | None = None, **overrides):
    """A cluster by name: a config entry, else a preset (``axon``), else a
    bare ssh host. With no name: $REMOTEEXEC_CLUSTER, then the config default."""
    from .clusters.presets import PRESETS
    from .clusters.slurm import SlurmCluster

    name = name or os.environ.get("REMOTEEXEC_CLUSTER") or None
    cfg = _find_config() or {}
    clusters = cfg.get("clusters", {})
    if name is None:
        name = cfg.get("default") or (next(iter(clusters)) if len(clusters) == 1 else None)
    if name is None:
        raise ValueError("no cluster given: pass -c NAME (e.g. -c axon), set "
                         "REMOTEEXEC_CLUSTER, or set `default` in remoteexec.toml")
    if name in clusters:
        return load_cluster(SlurmCluster, name, **overrides)
    if name in PRESETS:
        return PRESETS[name](**overrides)
    return SlurmCluster(host=name, **overrides)
