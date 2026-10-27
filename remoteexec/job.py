"""Job handle & live logging — §15."""
from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .errors import JobFailed
from .logtail import log_filename, read_chunk, resolve_raw_id
from .paths import rq
from .serialize import load_result


class JobState(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TIMEOUT = "TIMEOUT"
    UNKNOWN = "UNKNOWN"


TERMINAL = {JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED, JobState.TIMEOUT}


def _scratch_local(job_id: str) -> Path:
    p = Path("~/.remoteexec/results").expanduser() / f"{job_id}.result.pkl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _parse_state(raw: str) -> JobState:
    raw = (raw or "").strip().split()[0] if raw.strip() else ""
    # sacct uses e.g. "CANCELLED by 123"
    raw = raw.split("+")[0].upper()
    try:
        return JobState(raw)
    except ValueError:
        return JobState.UNKNOWN


@dataclass
class Job:
    cluster: "object"
    job_id: str
    session: str
    workdir: str            # shared project workspace: cwd + fetch() base
    logdir: str
    name: str
    rundir: str | None = None     # per-run dir (.rx/<session>) for payload/result
    result_path: str | None = None  # explicit override; else {rundir}/result.pkl

    @property
    def logfile(self) -> str:
        """Combined log path.

        Array tasks are named after their Slurm allocation number rather than
        the ``<array>_<index>`` id, so that has to be looked up once (see
        :func:`remoteexec.logtail.log_filename`).
        """
        raw = getattr(self, "_raw_id", None)
        if raw is None:
            raw = resolve_raw_id(self.cluster.conn, self.job_id)
            self._raw_id = raw
        return log_filename(self.logdir, self.job_id, raw)

    @property
    def _result_remote(self) -> str:
        if self.result_path:
            return self.result_path
        base = self.rundir or self.workdir
        return f"{base}/result.pkl"

    def state(self) -> JobState:
        conn = self.cluster.conn
        out = conn.run(
            f"sacct -j {self.job_id} --format=State -Pn 2>/dev/null | head -n1",
            check=False,
        ).stdout
        st = _parse_state(out)
        if st is JobState.UNKNOWN:
            # fallback to squeue (still pending/running, not yet in sacct)
            q = conn.run(
                f"squeue -j {self.job_id} -h -o %T 2>/dev/null", check=False
            ).stdout
            if q.strip():
                st = _parse_state(q)
        return st

    def done(self) -> bool:
        return self.state() in TERMINAL

    def wait(self, poll: float = 10.0, timeout=None) -> JobState:
        start = time.time()
        interval = min(poll, 3.0)
        while True:
            st = self.state()
            if st in TERMINAL:
                return st
            if timeout is not None and time.time() - start > timeout:
                return st
            time.sleep(interval)
            interval = min(interval * 1.5, poll)

    def result(self, **wait_kw):
        st = self.wait(**wait_kw)
        if st is not JobState.COMPLETED:
            raise JobFailed(state=st, log_tail=self.logs(tail=60))
        local = self.cluster.load_from_remote(
            self._result_remote, _scratch_local(self.job_id)
        )
        return load_result(local)

    def logs(self, tail: int | None = None) -> str:
        c = (
            f"tail -n {tail} {rq(self.logfile)}"
            if tail
            else f"cat {rq(self.logfile)}"
        )
        return self.cluster.conn.run(c, check=False).stdout

    def stream(self, follow: bool = True, poll: float = 2.0) -> None:
        """Live-tail the combined log to local stdout until the job is terminal."""
        conn = self.cluster.conn
        self.cluster.connect()
        while not conn.exists(self.logfile):
            if self.done():
                # Job finished before the log was visible (fast job + NFS lag).
                # Retry a few times so short jobs still show their captured log.
                for _ in range(5):
                    if conn.exists(self.logfile):
                        break
                    time.sleep(1.0)
                print(self.logs() or "(no output)")
                return
            time.sleep(poll)
        if not follow:
            print(self.logs())
            return
        # NFS-safe incremental tail. A persistent `tail -f` on the login node
        # does NOT reliably see the bytes the compute node appends, because NFS
        # attribute caching hides cross-node growth from the long-lived open fd.
        # So we re-read from a tracked byte offset each poll: every read is a
        # fresh open() that forces NFS to revalidate and fetch the new bytes.
        # read_chunk owns the offset arithmetic — see remoteexec/logtail.py for
        # why counting the decoded text instead replays output.
        logfile = self.logfile  # resolved once; the array lookup is not free
        offset = 0
        finishing = False
        while True:
            chunk = read_chunk(conn, logfile, offset)
            if chunk.text:
                sys.stdout.write(chunk.text)
                sys.stdout.flush()
            offset = chunk.next_offset
            if chunk.more:
                continue  # backlog still draining; don't sleep on it
            if finishing:
                break
            if self.done():
                finishing = True  # one more read to flush the remainder
                continue
            time.sleep(poll)

    def fetch(self, remote_subpath, local=None) -> Path:
        return self.cluster.load_from_remote(
            f"{self.workdir}/{remote_subpath}", local
        )

    def cancel(self) -> None:
        self.cluster.cancel(self.job_id)


class JobArray:
    def __init__(self, jobs: list[Job]):
        self.jobs = jobs

    def wait(self):
        return [j.wait() for j in self.jobs]

    def results(self) -> list:
        return [j.result() for j in self.jobs]

    def stream(self, task: int = 0):
        self.jobs[task].stream()
