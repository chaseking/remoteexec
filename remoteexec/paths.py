"""Remote-path handle + quoting helpers — §8."""
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
