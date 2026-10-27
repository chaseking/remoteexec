from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from remoteexec.connection import SSHConnection

HERE = Path(__file__).parent


class LocalShell(SSHConnection):
    """ssh/rsync swapped for a local shell: the "remote" is this machine."""

    @property
    def alias(self):
        return self.host

    def _ssh_argv(self, cmd, alias=None):
        return ["bash", "-c", cmd]

    def _rsync_shell(self):
        return []

    def _remote(self, path):
        return path

    def probe(self, alias=None, timeout=8):
        return True, 0.0, ""


@pytest.fixture
def rx_home(tmp_path, monkeypatch):
    monkeypatch.setenv("REMOTEEXEC_HOME", str(tmp_path / "rxhome"))
    monkeypatch.delenv("REMOTEEXEC_LOCAL", raising=False)
    return tmp_path / "rxhome"


@pytest.fixture
def fake_slurm(tmp_path, monkeypatch, rx_home):
    """Fake sbatch/sacct/squeue/scancel + `python` on PATH."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for cmd in ("sbatch", "sacct", "squeue", "scancel"):
        f = bindir / cmd
        f.write_text(f'#!/bin/bash\nexec {sys.executable} {HERE / "fakeslurm.py"} {cmd} "$@"\n')
        f.chmod(0o755)
    py = bindir / "python"  # a wrapper, not a symlink: a symlink loses the venv
    py.write_text(f'#!/bin/bash\nexec {sys.executable} "$@"\n')
    py.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKESLURM_DIR", str(tmp_path / "slurm"))
    return tmp_path


@pytest.fixture
def project(tmp_path, monkeypatch):
    """parent/ with shared api.py and two sibling work dirs."""
    proj = tmp_path / "parent"
    for d in ("dir1", "dir2"):
        (proj / d).mkdir(parents=True)
        (proj / d / "task.py").write_text(f"WHO = {d!r}\n")
    (proj / "pyproject.toml").write_text("[project]\nname = 'parent'\n")
    (proj / "api.py").write_text("def double(x):\n    return 2 * x\n")
    monkeypatch.chdir(proj)
    return proj


def make_cluster(tmp_path, **kw):
    from remoteexec import SlurmCluster

    kw.setdefault("check_env", False)
    kw.setdefault("verbose", False)
    c = SlurmCluster("fake", remote_root=str(tmp_path / "remote"), **kw)
    c.conn = LocalShell("fake")
    return c
