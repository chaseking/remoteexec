"""Job handles: state, logs, results and output files."""
from __future__ import annotations

import io
import re
import sys
import tarfile
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .accounting import batch_status
from .errors import JobFailed, JobTimeout, RemoteExecError
from .logtail import log_filename, read_chunk, resolve_raw_id
from .paths import RemotePath, list_files, rq, safe_relpath
from .serialize import unpack_result


class JobState(str, Enum):
    PENDING = "PENDING"
    CONFIGURING = "CONFIGURING"
    RUNNING = "RUNNING"
    COMPLETING = "COMPLETING"
    SUSPENDED = "SUSPENDED"
    REQUEUED = "REQUEUED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TIMEOUT = "TIMEOUT"
    OUT_OF_MEMORY = "OUT_OF_MEMORY"
    NODE_FAIL = "NODE_FAIL"
    BOOT_FAIL = "BOOT_FAIL"
    DEADLINE = "DEADLINE"
    PREEMPTED = "PREEMPTED"
    UNKNOWN = "UNKNOWN"


TERMINAL = {JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED,
            JobState.TIMEOUT, JobState.OUT_OF_MEMORY, JobState.NODE_FAIL,
            JobState.BOOT_FAIL, JobState.DEADLINE, JobState.PREEMPTED}

# A job neither sacct nor squeue knows about for this long is not coming back.
UNKNOWN_GIVE_UP = 120.0

# sacct first (it knows finished jobs), squeue for ones accounting hasn't seen.
_STATE_CMD = (
    "s=$(sacct -j {id} --format=State -Pn 2>/dev/null | head -n1); "
    '[ -n "$s" ] || s=$(squeue -j {id} -h -o %T 2>/dev/null | head -n1); echo "$s"'
)


def _parse_state(raw: str) -> JobState:
    raw = (raw or "").strip()
    raw = raw.split()[0] if raw else ""  # sacct says e.g. "CANCELLED by 123"
    try:
        return JobState(raw.split("+")[0].upper())
    except ValueError:
        return JobState.UNKNOWN


def slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip("-")[:40] or "job"


@dataclass(repr=False)
class Job:
    cluster: "object"
    job_id: str
    session: str
    workdir: str  # remote cwd of the job; fetch() base
    logdir: str
    name: str
    rundir: str | None = None  # per-run dir (.rx/<session>): runner, payload, logs, out
    result_path: str | None = None  # explicit override; else {rundir}/result.pkl
    outdir: str | None = None  # this job's output_dir() on the cluster
    kind: str = "fn"  # "fn" (pickled function) or "cmd" (shell command)
    local_base: str | None = None  # where fetch_outputs() lands by default

    def __repr__(self) -> str:
        host = getattr(getattr(self.cluster, "conn", None), "host", "?")
        return f"Job({self.name!r}, id={self.job_id}, host={host})"

    @property
    def conn(self):
        self.cluster.connect()
        return self.cluster.conn

    @property
    def logfile(self) -> str:
        """Combined log path. Array tasks are named after their allocation
        number, which has to be looked up once (see logtail.log_filename)."""
        raw = getattr(self, "_raw_id", None)
        if raw is None:
            raw = self._raw_id = resolve_raw_id(self.conn, self.job_id)
        return log_filename(self.logdir, self.job_id, raw)

    @property
    def _result_remote(self) -> str:
        return self.result_path or f"{self.rundir or self.workdir}/result.pkl"

    # -- state --
    def state(self) -> JobState:
        return _parse_state(self.conn.run(_STATE_CMD.format(id=self.job_id), check=False).stdout)

    def done(self) -> bool:
        return self.state() in TERMINAL

    def wait(self, poll: float = 10.0, timeout=None) -> JobState:
        """Block until the job finishes (or ``timeout`` seconds pass)."""
        start = time.time()
        interval = min(poll, 1.0)
        unknown_since = None
        while True:
            st = self.state()
            if st in TERMINAL:
                return st
            if st is JobState.UNKNOWN:
                unknown_since = unknown_since or time.time()
                if time.time() - unknown_since > UNKNOWN_GIVE_UP:
                    return st
            else:
                unknown_since = None
            if timeout is not None and time.time() - start > timeout:
                return st
            time.sleep(interval)
            interval = min(interval * 1.5, poll)

    def result(self, **wait_kw):
        """Wait, then return the function's value. Raises RemoteExecutionError
        if the function raised, JobFailed if Slurm ended the job otherwise,
        JobTimeout if ``timeout`` ran out first."""
        if hasattr(self, "_result"):
            return self._result
        st = self.wait(**wait_kw)
        if st not in TERMINAL and st is not JobState.UNKNOWN:
            raise JobTimeout(st, wait_kw.get("timeout"))
        if self.kind == "cmd":
            if st is not JobState.COMPLETED:
                raise JobFailed(state=st, log_tail=self.logs(tail=60))
            return None
        data = None
        if st in (JobState.COMPLETED, JobState.FAILED):
            got = self.conn.run(f"cat {rq(self._result_remote)}", check=False, text=False)
            data = got.stdout if got.returncode == 0 and got.stdout else None
        if data is None:
            raise JobFailed(state=st, log_tail=self.logs(tail=60))
        self._result = unpack_result(data)  # re-raises the remote exception
        return self._result

    # -- logs --
    def logs(self, tail: int | None = None) -> str:
        c = f"tail -n {int(tail)} {rq(self.logfile)}" if tail else f"cat {rq(self.logfile)}"
        return self.conn.run(c, check=False).stdout

    def stream(self, follow: bool = True, poll: float = 2.0) -> None:
        """Print the combined log as it grows, until the job ends."""
        conn = self.conn
        while not conn.exists(self.logfile):
            if self.done():
                # Finished before the log was visible (fast job + NFS lag).
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
        # Re-read from a byte offset each poll instead of holding `tail -f`:
        # over NFS a long-lived fd misses what the compute node appends, while
        # a fresh open() revalidates. See logtail.py for the offset rules.
        logfile = self.logfile
        offset, finishing = 0, False
        while True:
            chunk = read_chunk(conn, logfile, offset)
            if chunk.text:
                sys.stdout.write(chunk.text)
                sys.stdout.flush()
            offset = chunk.next_offset
            if chunk.more:
                continue
            if finishing:
                break
            if self.done():
                finishing = True  # one more read to flush the remainder
                continue
            time.sleep(poll)

    # -- files --
    @property
    def output_dir(self) -> RemotePath | None:
        """Remote handle of this job's ``output_dir()``."""
        return RemotePath(self.cluster, self.outdir) if self.outdir else None

    def outputs(self) -> dict[str, int]:
        """{relative path: size} of the files the job wrote to output_dir()."""
        if not self.outdir:
            return {}
        return {f["path"]: f["size"] for f in list_files(self.conn, self.outdir) or []}

    def read_output(self, relpath: str) -> str:
        """Text of one output file, without downloading anything else."""
        path = f"{self.outdir}/{safe_relpath(relpath)}"
        r = self.conn.run(f"cat {rq(path)}", check=False)
        if r.returncode:
            raise FileNotFoundError(f"{self.conn.host}:{path}")
        return r.stdout

    def fetch_outputs(self, local=None) -> Path:
        """rsync output_dir() down (default ``./remoteexec-out/<name>-<id>``)."""
        if not self.outdir:
            raise FileNotFoundError(f"job {self.job_id} has no output dir (submitted by remoteexec < 0.3)")
        dst = Path(local) if local else self.default_local_dir()
        dst.mkdir(parents=True, exist_ok=True)
        self.conn.rsync(f"{self.outdir}/", f"{dst}/", up=False)
        return dst

    def default_local_dir(self) -> Path:
        base = Path(self.local_base) if self.local_base and Path(self.local_base).is_dir() else Path.cwd()
        return base / "remoteexec-out" / f"{slug(self.name)}-{self.job_id}"

    def fetch(self, remote_subpath, local=None) -> Path:
        """rsync a path relative to the job's remote cwd."""
        return self.cluster.load_from_remote(f"{self.workdir}/{remote_subpath}", local)

    def cancel(self) -> None:
        self.cluster.cancel(self.job_id)


class JobArray:
    """Tasks of one ``.map`` submission. Status and results are fetched for
    all tasks in one round trip each, not one per task."""

    def __init__(self, jobs: list):
        self.jobs = jobs

    def __len__(self):
        return len(self.jobs)

    def __iter__(self):
        return iter(self.jobs)

    def __getitem__(self, i):
        return self.jobs[i]

    def __repr__(self):
        head = self.jobs[0].job_id.split("_")[0] if self.jobs else "-"
        name = self.jobs[0].name.split("[")[0] if self.jobs else ""
        return f"JobArray({name!r}, id={head}, tasks={len(self.jobs)})"

    def _remote(self) -> bool:
        return bool(self.jobs) and all(isinstance(j, Job) for j in self.jobs)

    def states(self) -> list[JobState]:
        if not self._remote():
            return [j.state() for j in self.jobs]
        info = batch_status(self.jobs[0].conn, [j.job_id for j in self.jobs])
        return [_parse_state(info.get(j.job_id, {}).get("state", "")) for j in self.jobs]

    def wait(self, poll: float = 10.0, timeout=None) -> list[JobState]:
        if not self._remote():
            return [j.wait() for j in self.jobs]
        start, interval = time.time(), min(poll, 1.0)
        while True:
            states = self.states()
            if all(s in TERMINAL for s in states):
                return states
            if timeout is not None and time.time() - start > timeout:
                return states
            time.sleep(interval)
            interval = min(interval * 1.5, poll)

    def results(self, return_exceptions: bool = False, **wait_kw) -> list:
        """Results in input order. A failed task raises (or, with
        ``return_exceptions=True``, appears as its exception)."""
        if not self._remote():
            return [j.result() for j in self.jobs]
        states = self.wait(**wait_kw)
        blobs = self._fetch_results(
            [j for j, s in zip(self.jobs, states) if s in (JobState.COMPLETED, JobState.FAILED)])
        out = []
        for job, st in zip(self.jobs, states):
            try:
                if st not in TERMINAL:
                    raise JobTimeout(st, wait_kw.get("timeout"))
                data = blobs.get(job.job_id)
                if data is None:
                    raise JobFailed(state=st, log_tail=job.logs(tail=60))
                out.append(unpack_result(data))
            except RemoteExecError as e:
                if not return_exceptions:
                    raise
                out.append(e)
        return out

    def _fetch_results(self, jobs) -> dict[str, bytes]:
        if not jobs:
            return {}
        paths = {j._result_remote.lstrip("/"): j.job_id for j in jobs}
        r = jobs[0].conn.run(
            "cd / && tar cf - --ignore-failed-read " + " ".join(rq(p) for p in paths)
            + " 2>/dev/null", check=False, text=False)
        found = {}
        try:
            with tarfile.open(fileobj=io.BytesIO(r.stdout)) as tf:
                for m in tf.getmembers():
                    if m.name in paths and m.isfile():
                        found[paths[m.name]] = tf.extractfile(m).read()
        except tarfile.TarError:
            pass
        return found

    def stream(self, task: int = 0):
        self.jobs[task].stream()

    def cancel(self):
        if self._remote():  # scancel <array id> hits every task
            self.jobs[0].cluster.cancel(self.jobs[0].job_id.split("_")[0])


class LocalJob:
    """A job that already ran on this machine (LocalCluster, ``run_local=True``
    or ``REMOTEEXEC_LOCAL=1``); mirrors the Job API that makes sense locally."""

    job_id = "local"

    def __init__(self, result, name, outdir=None):
        self._result = result
        self.name = name
        self.outdir = str(outdir) if outdir else None

    def __repr__(self):
        return f"LocalJob({self.name!r})"

    def state(self):
        return JobState.COMPLETED

    def done(self):
        return True

    def wait(self, *a, **kw):
        return JobState.COMPLETED

    def result(self, *a, **kw):
        return self._result

    def stream(self, *a, **kw):
        print("(local run — output was printed as it ran)")

    def logs(self, *a, **kw):
        return ""

    def outputs(self) -> dict[str, int]:
        if not self.outdir or not Path(self.outdir).is_dir():
            return {}
        root = Path(self.outdir)
        return {p.relative_to(root).as_posix(): p.stat().st_size
                for p in sorted(root.rglob("*")) if p.is_file()}

    def fetch_outputs(self, local=None) -> Path:
        return Path(self.outdir) if self.outdir else Path.cwd()
