"""What code gets rsynced to the remote workspace, and when it can be skipped.

Scopes (``SlurmCluster(sync=...)``, per function, or ``--sync`` on the CLI):

* ``True`` / ``"all"`` — the whole project root (default).
* ``"cwd"`` — files directly in the root (and in each directory between the
  root and the cwd) plus the cwd subtree; sibling directories are left alone,
  and the job runs from the mirrored cwd. For several agents working in
  ``parent/dir1`` and ``parent/dir2`` against shared ``parent/*.py``: they
  share one remote copy of ``parent``, and neither touches the other's dir.
* a list of root-relative paths/globs — exactly those.
* ``False`` / ``"none"`` — don't sync.

Every sync writes a marker named after a signature of the local tree. The
next submission sends that signature along; if the marker is still there the
remote already holds exactly this code and rsync is skipped entirely.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

DEFAULT_EXCLUDES = [
    ".git/", "__pycache__/", "*.pyc", ".venv/", "node_modules/",
    ".ipynb_checkpoints/", ".remoteexec/", "*.egg-info/", ".mypy_cache/",
    ".pytest_cache/", ".DS_Store",
    ".rx/",  # per-run dirs live here remotely; never sync/clobber them
    "remoteexec-out/",  # fetched outputs
]

_WILD = set("*?[")


def normalize(scope):
    if scope is None or scope is True or scope == "all":
        return "all"
    if scope is False or scope == "none":
        return "none"
    if scope == "cwd":
        return "cwd"
    if isinstance(scope, str):
        return [scope]
    return list(scope)


def gitignore_excludes(root: Path) -> list[str]:
    gi = root / ".gitignore"
    if not gi.exists():
        return []
    out = []
    for line in gi.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def cwd_rel(root: Path, cwd: Path) -> str:
    """cwd relative to root ("" when equal or outside)."""
    try:
        rel = cwd.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return ""
    return "" if rel == "." else rel


def _parents(path: str) -> list[str]:
    parts = path.strip("/").split("/")
    return ["/" + "/".join(parts[:i]) + "/" for i in range(1, len(parts))]


def filter_args(scope, rel: str = "") -> list[str]:
    """rsync include/exclude args restricting the transfer to ``scope``.

    Must come *after* the plain excludes: rsync uses the first matching rule.
    Uses ``dir/`` + ``dir/**`` rather than ``dir/***``, which macOS's old
    rsync and openrsync may not understand.
    """
    scope = normalize(scope)
    if scope in ("all", "none"):
        return []
    args: list[str] = []
    if scope == "cwd":
        if not rel:
            return []
        for p in [*_parents(rel), f"/{rel}/", f"/{rel}/**"]:
            args += ["--include", p]
        parts = rel.split("/")
        for i in range(len(parts) - 1, -1, -1):  # siblings at every level
            args += ["--exclude", "/" + "".join(f"{p}/" for p in parts[:i]) + "*/"]
        return args
    for pat in scope:
        pat = pat.strip("/")
        for p in [*_parents(pat), f"/{pat}", f"/{pat}/**"]:
            if p not in args:
                args += ["--include", p]
    return args + ["--exclude", "*"]


def _prunes(excludes) -> tuple[set, set]:
    """Directory names (any depth) and root-relative paths that rsync would
    skip, so the signature walk can skip them too. Only literal patterns —
    when unsure, walk (a spurious re-sync is cheap, a missed one is not)."""
    names, paths = set(), set()
    for e in excludes:
        if e.startswith("!") or _WILD & set(e):
            continue
        anchored = e.startswith("/")
        e = e.strip("/")
        if not e:
            continue
        if "/" in e or anchored:
            paths.add(e)
        else:
            names.add(e)
    return names, paths


def tree_signature(root: Path, scope, rel: str, excludes) -> str:
    """Hash of (path, size, mtime) for every file the scope could sync."""
    root = root.resolve()
    names, paths = _prunes(excludes)
    h = hashlib.blake2b(digest_size=12)

    def add_file(p: Path):
        try:
            st = p.lstat()
        except OSError:
            return
        h.update(f"{p.relative_to(root).as_posix()}\0{st.st_size}\0{st.st_mtime_ns}\n".encode())

    def walk(top: Path):
        for dirpath, dirnames, filenames in os.walk(top):
            here = Path(dirpath)
            relhere = here.relative_to(root).as_posix()
            dirnames[:] = sorted(
                d for d in dirnames
                if d not in names and (f"{relhere}/{d}" if relhere != "." else d) not in paths
            )
            for f in sorted(filenames):
                add_file(here / f)

    scope = normalize(scope)
    h.update(json.dumps([scope, rel, sorted(excludes)]).encode())
    if scope == "cwd" and rel:
        level = root
        for part in ["", *rel.split("/")[:-1]]:
            level = level / part if part else level
            for f in sorted(level.iterdir()):
                if not f.is_dir() or f.is_symlink():
                    add_file(f)
        walk(root / rel)
    else:
        walk(root)
    return h.hexdigest()
