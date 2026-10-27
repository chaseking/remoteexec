"""SSH transport: subprocess ``ssh``/``rsync`` sharing one ControlMaster socket."""
from __future__ import annotations

import hashlib
import os
import shlex
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import hosts, state
from .errors import ConnectionError, RemoteCommandError, TransferError
from .paths import rq

# cluster name -> alias picked by "auto" routing, for this process
_auto: dict[str, str] = {}


@dataclass
class CompletedRun:
    returncode: int
    stdout: str | bytes  # bytes when run with text=False
    stderr: str | bytes


@dataclass
class SSHConnection:
    host: str  # ssh alias or a cluster name registered in hosts.json
    user: str | None = None
    port: int | None = None
    identity_file: str | None = None
    control_dir: Path = field(default_factory=lambda: state.home() / "cm")
    control_persist: str = "10m"
    connect_timeout: int = 20
    extra_ssh_opts: list[str] = field(default_factory=list)

    @property
    def alias(self) -> str:
        """The ssh alias actually dialed (see :mod:`remoteexec.hosts`)."""
        name, aliases, active = hosts.lookup(self.host)
        if self.host != name:  # a specific alias was asked for: honor it
            return self.host
        if active != "auto":
            return active
        if name not in _auto:
            _auto[name] = next((a for a in aliases if self.probe(a, timeout=5)[0]),
                               aliases[0])
        return _auto[name]

    def target(self, alias: str | None = None) -> str:
        a = alias or self.alias
        return f"{self.user}@{a}" if self.user else a

    def _socket(self, alias: str) -> Path:
        sock = self.control_dir / f"{alias}-{self.port or 22}"
        if len(str(sock)) > 90:  # unix sockets cap paths at ~104 bytes
            digest = hashlib.sha1(str(sock).encode()).hexdigest()[:16]
            sock = Path(tempfile.gettempdir()) / f"rx-{os.getuid()}" / digest
        sock.parent.mkdir(parents=True, exist_ok=True)
        return sock

    def _base_opts(self, alias: str | None = None) -> list[str]:
        sock = self._socket(alias or self.alias)
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

    # -- seams overridden by the local test transport --
    def _ssh_argv(self, cmd: str, alias: str | None = None) -> list[str]:
        alias = alias or self.alias
        return ["ssh", *self._base_opts(alias), self.target(alias), cmd]

    def _rsync_shell(self) -> list[str]:
        return ["-e", "ssh " + " ".join(shlex.quote(o) for o in self._base_opts())]

    def _remote(self, path: str) -> str:
        return f"{self.target()}:{path}"

    def _forget_auto(self) -> bool:
        """Drop a stale auto-routing choice; True if a retry is worthwhile."""
        name, aliases, active = hosts.lookup(self.host)
        if active == "auto" and self.host == name and len(aliases) > 1:
            return _auto.pop(name, None) is not None
        return False

    def run(self, cmd: str, *, check: bool = True, timeout=None,
            text: bool = True, input: bytes | None = None) -> CompletedRun:
        """Run *cmd* on the remote host; ``input`` is piped to its stdin.

        ``text=False`` returns raw bytes — needed whenever byte counts matter
        (see logtail.py). Output is never newline-translated.
        """
        def once():
            try:
                return subprocess.run(
                    self._ssh_argv(cmd), capture_output=True, timeout=timeout,
                    input=input, stdin=None if input is not None else subprocess.DEVNULL,
                )
            except FileNotFoundError as e:
                raise ConnectionError(f"ssh binary not found: {e}") from e
            except subprocess.TimeoutExpired as e:
                raise ConnectionError(
                    f"ssh to {self.target()} timed out after {timeout}s: {cmd[:200]}") from e

        proc = once()
        if proc.returncode == 255 and self._forget_auto():
            proc = once()
        out, err = proc.stdout, proc.stderr
        if text:
            out, err = out.decode("utf-8", "replace"), err.decode("utf-8", "replace")
        if check and proc.returncode != 0:
            err_s = err if text else err.decode("utf-8", "replace")
            if proc.returncode == 255:
                raise ConnectionError(self._unreachable_msg(err_s))
            out_s = out if text else out.decode("utf-8", "replace")
            raise RemoteCommandError(cmd, proc.returncode, out_s, err_s)
        return CompletedRun(proc.returncode, out, err)

    def _unreachable_msg(self, stderr: str) -> str:
        name, aliases, _ = hosts.lookup(self.host)
        others = [a for a in aliases if a != self.alias]
        hint = (f"switch alias: remoteexec host use {name} {others[0]}" if others else
                f"if {name} has another ssh alias (e.g. off-site), register it: "
                f"remoteexec host add {name} <alias>")
        return f"ssh {self.target()} failed: {stderr.strip()[-500:]}\n({hint})"

    def popen(self, cmd: str) -> subprocess.Popen:
        return subprocess.Popen(self._ssh_argv(cmd), stdout=subprocess.PIPE,
                                stdin=subprocess.DEVNULL, text=True, bufsize=1)

    def exists(self, remote_path: str) -> bool:
        return self.run(f"test -e {rq(remote_path)}", check=False).returncode == 0

    def rsync(self, src, dst, *, up: bool, excludes=(), delete=False, extra=()):
        def once():
            argv = ["rsync", "-az", *self._rsync_shell()]
            for e in excludes:
                argv += ["--exclude", e]
            if delete:
                argv.append("--delete")
            argv += list(extra)
            argv += [str(src), self._remote(dst)] if up else [self._remote(src), str(dst)]
            try:
                return argv, subprocess.run(argv, capture_output=True, text=True,
                                            stdin=subprocess.DEVNULL)
            except FileNotFoundError as e:
                raise TransferError(f"rsync binary not found: {e}") from e

        argv, proc = once()
        if proc.returncode in (12, 255) and self._forget_auto():
            argv, proc = once()
        if proc.returncode != 0:
            raise TransferError(
                f"rsync failed (rc={proc.returncode}): {' '.join(argv)}\n{proc.stderr}")
        return proc

    def probe(self, alias: str | None = None, timeout: int = 8) -> tuple[bool, float, str]:
        """(reachable, milliseconds, error) for one alias, without prompting."""
        alias = alias or self.alias
        argv = self._ssh_argv("true", alias)
        # ssh keeps the first value of an option, so these go in front
        argv[1:1] = ["-o", "BatchMode=yes", "-o", f"ConnectTimeout={timeout}"]
        t0 = time.monotonic()
        try:
            p = subprocess.run(argv, capture_output=True, text=True,
                               timeout=timeout + 5, stdin=subprocess.DEVNULL)
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            return False, (time.monotonic() - t0) * 1000, str(e)
        ms = (time.monotonic() - t0) * 1000
        if p.returncode == 0:
            return True, ms, ""
        lines = p.stderr.strip().splitlines()
        return False, ms, lines[-1] if lines else f"ssh exited {p.returncode}"

    def open(self):
        """Warm the ControlMaster socket (one-time auth if any)."""
        self.run("true")
