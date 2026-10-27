"""SlurmCluster (concrete) — §8.1, §14."""
from __future__ import annotations

import logging
import secrets
import shlex
import sys
import time
import warnings
from pathlib import Path

from ..connection import SSHConnection
from ..environment import RawSetup, env_label
from ..errors import EnvParityWarning
from ..job import Job, JobArray, JobState, _parse_state
from ..paths import RemotePath, rq
from ..resources import SlurmResources
from ..serialize import pack_payload
from ..templates import render_sbatch
from .. import _runner, state
from .base import Cluster

log = logging.getLogger("remoteexec")

DEFAULT_EXCLUDES = [
    ".git/", "__pycache__/", "*.pyc", ".venv/", "node_modules/",
    ".ipynb_checkpoints/", ".remoteexec/", "*.egg-info/", ".mypy_cache/",
    ".DS_Store",
    ".rx/",  # per-run payload/result/log dirs live here; never sync/clobber them
]


def find_project_root(start: Path) -> Path:
    start = start.resolve()
    markers = {".git", "pyproject.toml", "setup.py", "setup.cfg", "remoteexec.toml"}
    for d in (start, *start.parents):
        if any((d / m).exists() for m in markers):
            return d
    return start


def new_session_id() -> str:
    return time.strftime("%Y%m%d-%H%M") + "-" + secrets.token_hex(3)


def expand_array_spec(jid: str) -> list[str]:
    """Expand squeue's compact array notation into individual task ids.

    Pending tasks are reported as ``123_[2-5]`` (or ``123_[0-9%2]`` with a
    throttle) rather than one row each, so a literal match against a manifest
    key like ``123_3`` would miss every pending task.
    """
    if "_[" not in jid or not jid.endswith("]"):
        return [jid]
    base, _, spec = jid.partition("_[")
    spec = spec[:-1].split("%")[0]  # drop the trailing ] and any %throttle
    out = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            lo, _, hi = part.partition("-")
            try:
                out.extend(f"{base}_{i}" for i in range(int(lo), int(hi) + 1))
            except ValueError:
                return [jid]
        elif part.isdigit():
            out.append(f"{base}_{part}")
    return out or [jid]


def batch_status(conn, job_ids, *, timeout=None, since=None):
    """One batched Slurm query for many jobs on a host.

    ``since`` (a ``datetime``) switches accounting from an explicit id list to
    a user+time-window scan, and the queue lookup from ``-j`` to ``-u``. The
    cost of ``sacct -j`` grows with the id list — 717 ids measured ~13s on a
    busy controller, against ~1s for one windowed scan — so callers holding a
    large manifest should pass the oldest time they still care about. Jobs
    outside the window come back absent (they are still picked up by the queue
    lookup while they are running).

    Returns {job_id: {"state": str, "start": str, "end": str,
                       "elapsed": str, "exitcode": str, "raw": str}}.
    Uses a single sacct call (main job rows only) plus a squeue fallback for
    jobs not yet visible in sacct accounting (freshly submitted / pending).
    ``raw`` is the JobIDRaw allocation number, which is what names an array
    task's log file (§ ``logtail.log_filename``); it comes along free here
    rather than costing a second query per job.
    ``timeout`` bounds each remote call, for callers (the GUI) that must not
    block indefinitely on an unresponsive login node.
    """
    job_ids = [str(j) for j in job_ids]
    if not job_ids:
        return {}
    wanted = set(job_ids)
    info: dict[str, dict] = {}

    fmt = "JobID,State,Start,End,Elapsed,ExitCode,JobIDRaw"
    if since is not None:
        # `now-2days` style offsets are not accepted by every Slurm build; an
        # absolute timestamp is.
        selector = f"-u $(whoami) -S {since.strftime('%Y-%m-%dT%H:%M:%S')}"
    else:
        selector = f"-j {','.join(job_ids)}"
    out = conn.run(
        f"sacct {selector} --format={fmt} -Pn 2>/dev/null",
        check=False, timeout=timeout,
    ).stdout
    for line in out.splitlines():
        parts = line.split("|")
        if len(parts) < 6:
            continue
        jid, state, start, end, elapsed, exitcode = parts[:6]
        if "." in jid:  # skip .batch/.extern sub-steps
            continue
        if jid not in wanted:  # a windowed scan sees the user's other jobs too
            continue
        info[jid] = {
            "state": state.split()[0] if state.strip() else "",
            "start": start, "end": end,
            "elapsed": elapsed, "exitcode": exitcode,
            "raw": parts[6].strip() if len(parts) > 6 else "",
        }

    missing = [j for j in job_ids if j not in info or not info[j]["state"]]
    if missing:
        # Jobs accounting cannot answer for: freshly submitted, still pending,
        # or (with a window) started before it. The queue knows them all.
        sel = f"-u $(whoami)" if since is not None else f"-j {','.join(missing)}"
        q = conn.run(
            f"squeue {sel} -h -o '%i|%T|%M|%S' 2>/dev/null",
            check=False, timeout=timeout,
        ).stdout
        want_missing = set(missing)
        for line in q.splitlines():
            parts = line.split("|")
            if len(parts) < 3:
                continue
            state, elapsed = parts[1], parts[2]
            start = parts[3] if len(parts) > 3 else ""
            for jid in expand_array_spec(parts[0]):
                if jid not in want_missing:
                    continue
                info[jid] = {
                    "state": state.strip(), "start": start, "end": "",
                    "elapsed": elapsed, "exitcode": "", "raw": "",
                }
    return info


def env_problems(local, remote) -> list[str]:
    """Human-readable list of parity problems between (py_minor, cloudpickle_ver)
    tuples. By-value cloudpickle needs matching Python minor + cloudpickle major.
    Empty list means the two environments are compatible."""
    lpy, lcp = local
    rpy, rcp = remote
    problems = []
    if lpy != rpy:
        problems.append(f"Python {lpy} (local) vs {rpy} (remote)")
    if lcp.split(".")[0] != rcp.split(".")[0]:
        problems.append(f"cloudpickle {lcp} (local) vs {rcp} (remote)")
    return problems


def _gitignore_excludes(root: Path) -> list[str]:
    gi = root / ".gitignore"
    if not gi.exists():
        return []
    out = []
    for line in gi.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


class SlurmCluster(Cluster):
    def __init__(self, host, *, user=None, port=None, identity_file=None,
                 remote_root="~/remoteexec", project=None, local_dir=None,
                 env=None, local_env=None, default_resources=None, setup=(),
                 excludes=(), respect_gitignore=True, check_env=True):
        self.conn = SSHConnection(host, user=user, port=port,
                                  identity_file=identity_file)
        self.local_dir = (
            Path(local_dir).resolve() if local_dir
            else find_project_root(Path.cwd())
        )
        self.remote_root = remote_root
        self.project = project or self.local_dir.name
        self.env = env or RawSetup("")
        self.local_env = local_env
        self.setup = list(setup)
        self.default_resources = (
            default_resources if isinstance(default_resources, SlurmResources)
            else SlurmResources(**(default_resources or {}))
        )
        self.excludes = list(excludes)
        self.respect_gitignore = respect_gitignore
        self.check_env = check_env
        self._connected = False
        self._env_checked = False

    @property
    def project_root(self) -> str:
        return f"{self.remote_root}/{self.project}"

    def connect(self):
        if not self._connected:
            self.conn.open()
            self._resolve_remote_root()
            self._connected = True

    def _resolve_remote_root(self):
        # Slurm's `#SBATCH --output=` does not expand `~`, so resolve the remote
        # home to an absolute path once and use that everywhere.
        if self.remote_root.startswith("~"):
            home = self.conn.run("printf %s \"$HOME\"").stdout.strip()
            if home:
                self.remote_root = home + self.remote_root[1:]

    # ---- environment parity (§25/§26) ----
    def remote_env_versions(self, env=None):
        """(python_minor, cloudpickle_version) of the *remote* environment the
        job will run in, or None if it couldn't be determined. Activates the env
        exactly as the sbatch script does, then asks its python."""
        self.connect()
        use_env = env or self.env
        pycode = (
            "import sys, cloudpickle; "
            'print("REMOTEEXEC_ENV", "%d.%d" % sys.version_info[:2], '
            "cloudpickle.__version__)"
        )
        script = "\n".join(
            [use_env.activation_script(),
             f"{use_env.launcher()} -c {shlex.quote(pycode)}"]
        )
        out = self.conn.run(script, check=False).stdout
        for line in out.splitlines():
            if line.startswith("REMOTEEXEC_ENV"):
                _, py, cp = line.split()
                return py, cp
        return None  # cloudpickle missing or python failed to launch

    def _maybe_warn_env(self, env=None):
        if not self.check_env or self._env_checked:
            return
        self._env_checked = True
        try:
            import cloudpickle

            local = (f"{sys.version_info.major}.{sys.version_info.minor}",
                     cloudpickle.__version__)
            remote = self.remote_env_versions(env)
        except Exception:
            return  # never let the diagnostic break a submission
        if remote is None:
            warnings.warn(
                f"remoteexec: could not import cloudpickle in the remote env "
                f"({self.env.__class__.__name__}); the job will fail to "
                f"unpickle its payload. Install cloudpickle there (local has "
                f"{local[1]}).",
                EnvParityWarning, stacklevel=3,
            )
            return
        problems = env_problems(local, remote)
        if problems:
            warnings.warn(
                "remoteexec: local/remote environment mismatch — "
                + "; ".join(problems)
                + ". Functions are shipped by value with cloudpickle, so this "
                "can cause unpickling errors on the cluster. Match the Python "
                "minor version and cloudpickle major version, or write large "
                "artifacts to disk and fetch them instead of returning them.",
                EnvParityWarning, stacklevel=3,
            )

    # ---- paths & transfer (§8) ----
    def _resolve(self, p: str) -> str:
        p = str(p)
        return p if p[:1] in ("/", "~", "$") else f"{self.project_root}/{p}"

    def make_path(self, *parts, mkdir=True) -> RemotePath:
        remote = self._resolve("/".join(str(p) for p in parts))
        if mkdir:
            self.connect()
            self.conn.run(f"mkdir -p {rq(remote)}")
        return RemotePath(self, remote)

    def load_from_remote(self, remote, local=None) -> Path:
        self.connect()
        remote_abs = self._resolve(str(remote))
        dst = Path(local or (Path.cwd() / Path(remote_abs).name)).expanduser()
        dst.parent.mkdir(parents=True, exist_ok=True)
        self.conn.rsync(remote_abs, str(dst), up=False)
        return dst

    def save_to_remote(self, local, remote) -> RemotePath:
        self.connect()
        remote_abs = self._resolve(str(remote))
        self.conn.run(f"mkdir -p {rq(str(Path(remote_abs).parent))}")
        self.conn.rsync(str(Path(local).expanduser()), remote_abs, up=True)
        return RemotePath(self, remote_abs)

    def _all_excludes(self) -> list[str]:
        ex = list(DEFAULT_EXCLUDES)
        if self.respect_gitignore:
            ex += _gitignore_excludes(self.local_dir)
        ex += self.excludes
        return ex

    def _sync_up(self, workdir: str):
        # trailing slash -> copy *contents* of the project root into workdir
        self.conn.rsync(
            f"{self.local_dir}/", workdir, up=True,
            excludes=self._all_excludes(), delete=False,
        )

    def _upload_bytes(self, data: bytes, remote_path: str):
        import tempfile

        with tempfile.NamedTemporaryFile(delete=False) as tf:
            tf.write(data)
            tmp = tf.name
        try:
            self.conn.rsync(tmp, remote_path, up=True)
        finally:
            Path(tmp).unlink(missing_ok=True)

    def _upload_text(self, text: str, remote_path: str):
        self._upload_bytes(text.encode(), remote_path)

    def _upload_runner(self, remote_path: str):
        self.conn.rsync(str(Path(_runner.__file__)), remote_path, up=True)

    # ---- submission (§14) ----
    def submit(self, fn, args, kwargs, resources, *, env=None, name=None,
               setup=(), sync=True) -> Job:
        self.connect()
        self._maybe_warn_env(env)
        session = new_session_id()
        # Shared per-project workspace (synced incrementally, reused across runs)
        # + a tiny per-run dir for this job's payload/result/logs (§8.1, Update 2).
        workspace = self.project_root
        rundir = f"{self.project_root}/.rx/{session}"
        logdir = f"{rundir}/logs"
        self.conn.run(f"mkdir -p {rq(logdir)}")

        if sync:
            log.info("project root: %s -> %s:%s", self.local_dir,
                     self.conn.target(), workspace)
            self._sync_up(workspace)
        self._upload_bytes(pack_payload(fn, args, kwargs), f"{rundir}/payload.pkl")
        self._upload_runner(f"{rundir}/_runner.py")

        eff = self.default_resources.merge(resources)
        eff.job_name = eff.job_name or name or getattr(fn, "__name__", "job")
        use_env = env or self.env
        script = render_sbatch(
            eff, env=use_env, setup=[*self.setup, *setup],
            cd_dir=workspace, logdir=logdir,
            runner_path=f"{rundir}/_runner.py",
            payload_path=f"{rundir}/payload.pkl",
            result_path=f"{rundir}/result.pkl",
        )
        self._upload_text(script, f"{rundir}/run.sbatch")

        out = self.conn.run(f"sbatch --parsable {rq(rundir)}/run.sbatch").stdout
        job_id = out.split(";")[0].strip()

        job = Job(self, job_id=job_id, session=session, workdir=workspace,
                  logdir=logdir, name=eff.job_name, rundir=rundir)
        state.record(job, env=env_label(use_env), local_dir=str(self.local_dir),
                     partition=eff.partition)
        log.info(
            "submitted %r as job %s\n         log:    %s:%s/%s.out"
            "\n         stream: job.stream()    cli: remoteexec logs %s -f",
            eff.job_name, job_id, self.conn.target(), logdir, job_id, job_id,
        )
        return job

    def map(self, fn, iterable, *, resources, env=None, name=None,
            setup=(), sync=True, array_throttle=None) -> JobArray:
        self.connect()
        self._maybe_warn_env(env)
        items = list(iterable)
        session = new_session_id()
        workspace = self.project_root
        rundir = f"{self.project_root}/.rx/{session}"
        logdir = f"{rundir}/logs"
        self.conn.run(
            f"mkdir -p {rq(logdir)} {rq(rundir + '/payloads')} "
            f"{rq(rundir + '/results')}"
        )
        if sync:
            self._sync_up(workspace)
        for i, item in enumerate(items):
            if isinstance(item, dict):
                a, kw = (), item
            elif isinstance(item, tuple):
                a, kw = item, {}
            else:
                a, kw = (item,), {}
            self._upload_bytes(
                pack_payload(fn, a, kw), f"{rundir}/payloads/{i}.pkl"
            )
        self._upload_runner(f"{rundir}/_runner.py")

        eff = self.default_resources.merge(resources)
        eff.job_name = eff.job_name or name or getattr(fn, "__name__", "job")
        spec = f"0-{len(items) - 1}"
        if array_throttle:
            spec += f"%{array_throttle}"
        eff.array = spec
        use_env = env or self.env
        script = render_sbatch(
            eff, env=use_env, setup=[*self.setup, *setup],
            cd_dir=workspace, logdir=logdir,
            runner_path=f"{rundir}/_runner.py", array_dir=rundir,
        )
        self._upload_text(script, f"{rundir}/run.sbatch")
        out = self.conn.run(f"sbatch --parsable {rq(rundir)}/run.sbatch").stdout
        array_id = out.split(";")[0].strip()

        jobs = []
        label = env_label(use_env)
        for i in range(len(items)):
            jid = f"{array_id}_{i}"
            j = Job(self, job_id=jid, session=session,
                    workdir=workspace, logdir=logdir,
                    name=f"{eff.job_name}[{i}]",
                    rundir=rundir,
                    result_path=f"{rundir}/results/{i}.pkl")
            state.record(j, env=label, local_dir=str(self.local_dir),
                         partition=eff.partition)
            jobs.append(j)
        return JobArray(jobs)

    # ---- job control ----
    def status(self, job_id) -> JobState:
        out = self.conn.run(
            f"sacct -j {job_id} --format=State -Pn 2>/dev/null | head -n1",
            check=False,
        ).stdout
        return _parse_state(out)

    def cancel(self, job_id) -> None:
        self.connect()
        self.conn.run(f"scancel {job_id}", check=False)

    def job(self, job_id) -> Job:
        rec = state.get(str(job_id))
        if rec is None:
            raise KeyError(f"no manifest entry for job {job_id}")
        return Job(self, job_id=str(job_id), session=rec["session"],
                   workdir=rec["workdir"], logdir=rec["logdir"], name=rec["name"],
                   rundir=rec.get("rundir"))  # old entries: None -> falls back to workdir

    @classmethod
    def from_config(cls, name=None):
        from ..config import load_cluster
        return load_cluster(cls, name)
