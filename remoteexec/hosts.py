"""Cluster name -> ssh alias routing, stored in ~/.remoteexec/hosts.json.

One cluster is often reachable through several ssh aliases (``axon`` on-site,
``axon-remote`` off-site). Registering them under one name lets code keep
saying ``AxonCluster()`` while the actual hop is switched in one place
(``remoteexec host use axon axon-remote``, the GUI, or the menu bar app)::

    {"axon": {"hosts": ["axon", "axon-remote"], "active": "axon-remote"}}

``active`` may also be ``"auto"``: the first alias that answers is used.
Jobs are recorded under the cluster name, so they stay visible whichever
alias is active.
"""
from __future__ import annotations

from . import state

_cache: tuple[float, dict] | None = None


def _path():
    return state.home() / "hosts.json"


def load() -> dict:
    """{name: {"hosts": [...], "active": str}}; re-read when the file changes."""
    global _cache
    p = _path()
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return {}
    if _cache is None or _cache[0] != mtime:
        raw = state.read_json(p)
        clean = {}
        for name, spec in raw.items():
            hosts = [h for h in spec.get("hosts", []) if h] or [name]
            active = spec.get("active") or hosts[0]
            clean[name] = {"hosts": hosts, "active": active}
        _cache = (mtime, clean)
    return _cache[1]


def save(data: dict) -> None:
    global _cache
    state.write_json(_path(), data)
    _cache = None


def lookup(host: str) -> tuple[str, list[str], str]:
    """(cluster name, its aliases, active alias) for a host or alias.

    Unknown hosts route to themselves.
    """
    clusters = load()
    if host in clusters:
        spec = clusters[host]
        return host, spec["hosts"], spec["active"]
    for name, spec in clusters.items():
        if host in spec["hosts"]:
            return name, spec["hosts"], spec["active"]
    return host, [host], host


def canonical(host: str) -> str:
    return lookup(host)[0]


def set_hosts(name: str, hosts: list[str], active: str | None = None) -> None:
    data = dict(load())
    hosts = [h for h in dict.fromkeys(hosts) if h] or [name]
    active = active or data.get(name, {}).get("active")
    if active != "auto" and active not in hosts:
        active = hosts[0]
    data[name] = {"hosts": hosts, "active": active}
    save(data)


def use(name: str, alias: str) -> None:
    """Route cluster ``name`` through ``alias`` (or ``"auto"``)."""
    data = dict(load())
    spec = data.get(name) or {"hosts": [name], "active": name}
    hosts = list(spec["hosts"])
    if alias != "auto" and alias not in hosts:
        hosts.append(alias)
    data[name] = {"hosts": hosts, "active": alias}
    save(data)


def remove(name: str) -> None:
    data = dict(load())
    data.pop(name, None)
    save(data)
