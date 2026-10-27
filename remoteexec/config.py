"""TOML config — §17."""
from __future__ import annotations

import sys
from pathlib import Path

from .environment import ApptainerEnv, CondaEnv, ModuleEnv, RawSetup, UvEnv, VenvEnv

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

def _search_paths() -> list[Path]:
    """Config files in priority order, highest first.

    Project-local files override the global user config, so a repo can add or
    redefine clusters on top of your `~/.config` defaults. Project files are
    resolved from the *project root* (walked up from cwd), not just cwd.
    """
    from .clusters.slurm import find_project_root  # lazy: avoid import cycle

    root = find_project_root(Path.cwd())
    return [
        root / "remoteexec.toml",
        Path("~/.config/remoteexec/config.toml").expanduser(),
        root / "pyproject.toml",  # [tool.remoteexec]
    ]


def _read(p: Path) -> dict | None:
    data = tomllib.loads(p.read_text())
    if p.name == "pyproject.toml":
        data = data.get("tool", {}).get("remoteexec")
    return data or None


def _find_config() -> dict | None:
    """Merge every config source into one mapping.

    Sources are visited highest-priority first; a cluster name (and the
    top-level `default`) defined in a higher-priority file wins over the same
    key in a lower-priority one. Clusters from different files are unioned, so
    you can keep shared clusters in `~/.config` and project-specific ones in a
    `remoteexec.toml` at the project root.
    """
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
    if kind == "conda":
        return CondaEnv(spec["name"], pre=spec.get("pre", []),
                        init=spec.get("init", False))
    if kind == "uv":
        return UvEnv(**{k: v for k, v in spec.items() if k != "kind"})
    if kind == "venv":
        return VenvEnv(spec["path"])
    if kind == "module":
        return ModuleEnv(spec["modules"])
    if kind == "raw":
        return RawSetup(spec.get("script", ""))
    if kind == "apptainer":
        return ApptainerEnv(**{k: v for k, v in spec.items() if k != "kind"})
    raise ValueError(f"unknown env kind: {kind}")


def load_cluster(cls, name=None):
    cfg = _find_config()
    if cfg is None:
        raise FileNotFoundError("no remoteexec config found")
    name = name or cfg.get("default")
    clusters = cfg.get("clusters", {})
    if name is None:
        if len(clusters) != 1:
            raise ValueError(
                "specify a cluster name (no default set); "
                f"available: {sorted(clusters)}")
        name = next(iter(clusters))
    if name not in clusters:
        raise KeyError(
            f"cluster {name!r} not found in config; available: {sorted(clusters)}")
    c = clusters[name]
    return cls(
        host=c["host"],
        remote_root=c.get("remote_root", "~/remoteexec"),
        project=c.get("project"),
        local_dir=c.get("local_dir"),
        env=_build_env(c.get("env")),
        local_env=_build_env(c.get("local_env")),
        default_resources=c.get("default_resources"),
        setup=c.get("setup", ()),
        excludes=c.get("excludes", ()),
    )
