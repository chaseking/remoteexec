"""Remote paths: shell quoting, a path handle, and directory listing."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .clusters.slurm import SlurmCluster

# Characters that are safe to leave unquoted so the remote shell still expands
# `$VAR` and a leading `~`. Everything else falls back to a quoted form.
_SAFE_RE = re.compile(r"^[~$A-Za-z0-9_./%-]+$")


def rq(path: str) -> str:
    """Quote a remote path: let ``$VAR`` and leading ``~`` expand on the remote
    while protecting spaces and shell metacharacters."""
    path = str(path)
    if _SAFE_RE.match(path):
        return path
    escaped = path.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`")
    return f'"{escaped}"'


def list_files(conn, root: str, *, limit: int = 5000, timeout=None) -> list[dict] | None:
    """Files under ``root`` as ``[{"path", "size", "mtime"}]`` sorted by path,
    or None if ``root`` doesn't exist. One round trip (GNU find, else BSD stat)."""
    out = conn.run(
        f"cd {rq(root)} 2>/dev/null || {{ echo NOPE; exit 0; }}; "
        f"{{ if find . -maxdepth 0 -printf '' 2>/dev/null; then "
        f"find . -type f -printf '%s\\t%T@\\t%P\\n' 2>/dev/null; "
        f"else t=$(printf '\\t'); "  # BSD stat doesn't expand \t itself
        f"find . -type f -exec stat -f \"%z$t%m$t%N\" {{}} + 2>/dev/null; fi; }} "
        f"| head -n {int(limit)}",
        check=False, timeout=timeout,
    ).stdout
    if out.startswith("NOPE"):
        return None
    files = []
    for line in out.splitlines():
        parts = line.split("\t", 2)
        if len(parts) == 3:
            path = parts[2][2:] if parts[2].startswith("./") else parts[2]  # BSD %N keeps "./"
            files.append({"path": path, "size": int(parts[0]), "mtime": float(parts[1])})
    return sorted(files, key=lambda f: f["path"])


def safe_relpath(p: str) -> str:
    """Reject absolute paths and ``..`` so a caller can't escape a directory."""
    p = (p or "").strip()
    while p.startswith("./"):
        p = p[2:]
    if not p or p.startswith("/") or ".." in p.split("/"):
        raise ValueError(f"bad relative path: {p!r}")
    return p


@dataclass(frozen=True)
class RemotePath:
    cluster: "SlurmCluster"
    path: str  # remote-absolute (or $VAR/~ rooted) string

    def __str__(self) -> str:
        return self.path

    def __fspath__(self) -> str:
        return self.path

    def __truediv__(self, other) -> "RemotePath":
        return RemotePath(self.cluster, f"{self.path}/{other}")

    def fetch(self, local=None) -> Path:
        return self.cluster.load_from_remote(self.path, local)

    def push(self, local) -> "RemotePath":
        return self.cluster.save_to_remote(local, self.path)

    def exists(self) -> bool:
        return self.cluster.conn.exists(self.path)

    def ls(self) -> dict[str, int]:
        """{relative path: size} of the files under this directory."""
        self.cluster.connect()
        return {f["path"]: f["size"] for f in list_files(self.cluster.conn, self.path) or []}
