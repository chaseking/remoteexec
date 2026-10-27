"""SSH connection layer — §7. subprocess ssh/rsync + ControlMaster."""
from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ConnectionError, RemoteCommandError, TransferError
from .paths import rq


@dataclass
class CompletedRun:
    returncode: int
    stdout: str | bytes  # bytes when the command ran with text=False
    stderr: str | bytes


@dataclass
class SSHConnection:
    host: str  # the only required field (e.g. "axon")
    user: str | None = None
    port: int | None = None
    identity_file: str | None = None
    control_dir: Path = Path("~/.remoteexec/cm").expanduser()
    control_persist: str = "10m"
    connect_timeout: int = 20
    extra_ssh_opts: list[str] = field(default_factory=list)

    def target(self) -> str:
        return f"{self.user}@{self.host}" if self.user else self.host

    def _base_opts(self) -> list[str]:
        self.control_dir.mkdir(parents=True, exist_ok=True)
        sock = self.control_dir / f"{self.host}-{self.port or 22}"
        opts = [
            "-o", "ControlMaster=auto",
            "-o", f"ControlPath={sock}",
            "-o", f"ControlPersist={self.control_persist}",
            "-o", f"ConnectTimeout={self.connect_timeout}",
        ]
        if self.port:
            opts += ["-p", str(self.port)]
        if self.identity_file:
            opts += ["-i", self.identity_file]
        return opts + self.extra_ssh_opts

    def run(self, cmd: str, *, check: bool = True, timeout=None,
            text: bool = True) -> CompletedRun:
        """Run *cmd* on the remote host.

        ``text=False`` returns stdout/stderr as raw bytes — needed whenever the
        byte count matters, since text mode rewrites newlines (``\\r\\n`` and
        lone ``\\r`` both collapse to ``\\n``) and so cannot be measured against
        the file it came from.
        """
        argv = ["ssh", *self._base_opts(), self.target(), cmd]
        # errors="replace": a stray non-UTF-8 byte in a remote log or command
        # output should not blow up the caller with a UnicodeDecodeError.
        kw = {"text": True, "errors": "replace"} if text else {}
        try:
            proc = subprocess.run(
                argv, capture_output=True, timeout=timeout, **kw
            )
        except FileNotFoundError as e:  # ssh missing
            raise ConnectionError(f"ssh binary not found: {e}") from e
        except subprocess.TimeoutExpired as e:
            raise ConnectionError(
                f"ssh to {self.target()} timed out after {timeout}s: {cmd}"
            ) from e
        if check and proc.returncode != 0:
            out, err = proc.stdout, proc.stderr
            if not text:
                out = out.decode("utf-8", "replace")
                err = err.decode("utf-8", "replace")
            raise RemoteCommandError(cmd, proc.returncode, out, err)
        return CompletedRun(proc.returncode, proc.stdout, proc.stderr)

    def popen(self, cmd: str) -> subprocess.Popen:  # for `tail -f` streaming
        return subprocess.Popen(
            ["ssh", *self._base_opts(), self.target(), cmd],
            stdout=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

    def exists(self, remote_path: str) -> bool:
        return self.run(f"test -e {rq(remote_path)}", check=False).returncode == 0

    def rsync(self, src, dst, *, up: bool, excludes=(), delete=False, extra=()):
        ssh_cmd = "ssh " + " ".join(shlex.quote(o) for o in self._base_opts())
        argv = ["rsync", "-az", "-e", ssh_cmd]
        for e in excludes:
            argv += ["--exclude", e]
        if delete:
            argv.append("--delete")
        argv += list(extra)
        if up:
            argv += [str(src), f"{self.target()}:{dst}"]
        else:
            argv += [f"{self.target()}:{src}", str(dst)]
        try:
            proc = subprocess.run(argv, capture_output=True, text=True)
        except FileNotFoundError as e:
            raise TransferError(f"rsync binary not found: {e}") from e
        if proc.returncode != 0:
            raise TransferError(
                f"rsync failed (rc={proc.returncode}): {' '.join(argv)}\n{proc.stderr}"
            )
        return proc

    def open(self):
        """Warm the ControlMaster socket (one-time auth if any)."""
        self.run("true")
