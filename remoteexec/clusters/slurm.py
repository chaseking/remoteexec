"""SlurmCluster: sync code, submit jobs, reattach to them.

Remote layout::

    <remote_root>/<project>/                 workspace: mirror of local_dir (shared, incremental)
    <remote_root>/<project>/.rx/<session>/   one per submission
        run.sbatch  _runner.py  lib/  payload.pkl | payloads/<i>.pkl
        result.pkl | results/<i>.pkl  logs/<jobid>.out  out/ (| out/<i>/)

A submission is one ssh round trip (a gzip tarball piped into
``tar x && sbatch``), plus an rsync only when the local code changed.
"""
from __future__ import annotations

import hashlib
import io
import json
import secrets
import shlex
import sys
import tarfile
import time
import warnings
from dataclasses import dataclass
from pathlib import Path

from .. import _runner, state
from .. import sync as syncmod
from ..accounting import batch_status, expand_array_spec  # noqa: F401 (re-exported)
from ..connection import SSHConnection
from ..environment import RawSetup, env_label, wrap_command
from ..errors import EnvParityWarning, SubmissionError
from ..hosts import canonical
from ..job import _STATE_CMD, Job, JobArray, JobState, _parse_state, slug
from ..paths import RemotePath, rq
from ..resources import SlurmResources
from ..serialize import pack_payload
from ..templates import render_sbatch
from .base import Cluster

DEFAULT_EXCLUDES = syncmod.DEFAULT_EXCLUDES
_gitignore_excludes = syncmod.gitignore_excludes
SYNC_MISSING = 97  # exit status of the "workspace already has this code" guard
ENV_CHECK_TTL = 3 * 24 * 3600


def find_project_root(start: Path) -> Path:
    start = start.resolve()
    markers = {".git", "pyproject.toml", "setup.py", "setup.cfg", "remoteexec.toml"}
    for d in (start, *start.parents):
        if any((d / m).exists() for m in markers):
            return d
    return start


def new_session_id(name: str = "") -> str:
    tag = f"-{slug(name)[:24]}" if name else ""
    return time.strftime("%Y%m%d-%H%M%S") + tag + "-" + secrets.token_hex(3)


def env_problems(local, remote) -> list[str]:
    """Parity problems between (py_minor, cloudpickle_ver) tuples. By-value
    cloudpickle needs matching Python minor + cloudpickle major."""
    (lpy, lcp), (rpy, rcp) = local, remote
    problems = []
    if lpy != rpy:
        problems.append(f"Python {lpy} (local) vs {rpy} (remote)")
    if lcp.split(".")[0] != rcp.split(".")[0]:
        problems.append(f"cloudpickle {lcp} (local) vs {rcp} (remote)")
    return problems


_lib_cache: dict[str, bytes] | None = None


def _lib_files() -> dict[str, bytes]:
    """The package's .py files, shipped as a fallback import for the job."""
    global _lib_cache
    if _lib_cache is None:
        pkg = Path(__file__).resolve().parent.parent
        _lib_cache = {
            "lib/remoteexec/" + p.relative_to(pkg).as_posix(): p.read_bytes()
            for p in sorted(pkg.rglob("*.py")) if "__pycache__" not in p.parts
        }
    return _lib_cache


def _tarball(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", compresslevel=6) as tf:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o644
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _name_from_command(command: str) -> str:
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    for t in tokens:
        if t.endswith((".py", ".sh")):
            return Path(t).stem
    return Path(tokens[0]).name if tokens else "cmd"


@dataclass
class _SyncPlan:
    scope: object
    rel: str
    excludes: list
    sig: str
    key: str  # identifies (host, workspace, scope) in the local cache


class SlurmCluster(Cluster):
    def __init__(self, host, *, user=None, port=None, identity_file=None,
                 remote_root="~/remoteexec", project=None, local_dir=None,
                 env=None, local_env=None, default_resources=None, setup=(),
                 excludes=(), respect_gitignore=True, check_env=True,
                 sync=True, verbose=True):
        self.conn = SSHConnection(host, user=user, port=port, identity_file=identity_file)
        self.local_dir = Path(local_dir).resolve() if local_dir else find_project_root(Path.cwd())
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
        self.sync = syncmod.normalize(sync)  # default scope, see remoteexec.sync
        self.verbose = verbose
        self._connected = False
        self._env_checked = False
        self._root_made = False

    def __repr__(self):
        return f"{type(self).__name__}(host={self.conn.host!r}, project={self.project!r})"

    @property
    def project_root(self) -> str:
        return f"{self.remote_root}/{self.project}"

    def connect(self):
        if self._connected:
            return
        if self.remote_root.startswith("~"):
            # `#SBATCH --output=` doesn't expand `~`: resolve the home once.
            # This round trip doubles as the ControlMaster warm-up.
            home = self.conn.run('printf %s "$HOME"').stdout.strip()
            if home:
                self.remote_root = home + self.remote_root[1:]
        else:
            self.conn.open()
        self._connected = True

    def _say(self, msg: str):
        if self.verbose:
            print(f"[remoteexec] {msg}", file=sys.stderr, flush=True)

    # ---- environment parity ----
    def remote_env_versions(self, env=None):
        """(python_minor, cloudpickle_version) of the remote env as the job
        will see it, or None if python/cloudpickle couldn't be loaded."""
        self.connect()
        use_env = env or self.env
        pycode = ('import sys, cloudpickle; print("REMOTEEXEC_ENV", '
                  '"%d.%d" % sys.version_info[:2], cloudpickle.__version__)')
        script = f"{use_env.activation_script()}\n{use_env.launcher()} -c {shlex.quote(pycode)}"
        for line in self.conn.run(script, check=False).stdout.splitlines():
            if line.startswith("REMOTEEXEC_ENV"):
                _, py, cp = line.split()
                return py, cp
        return None

    def _maybe_warn_env(self, env=None):
        """Warn once if pickles made here can't be read there. A passing check
        is cached on disk for a few days, so it costs a round trip rarely."""
        if not self.check_env or self._env_checked:
            return
        self._env_checked = True
        try:
            import cloudpickle

            use_env = env or self.env
            local = (f"{sys.version_info.major}.{sys.version_info.minor}", cloudpickle.__version__)
            key = hashlib.sha1(json.dumps([
                canonical(self.conn.host), use_env.activation_script(), use_env.launcher(), local,
            ]).encode()).hexdigest()[:16]
            cache_path = state.home() / "envcheck.json"
            cache = state.read_json(cache_path)
            if time.time() - cache.get(key, 0) < ENV_CHECK_TTL:
                return
            remote = self.remote_env_versions(env)
        except Exception:
            return  # never let the diagnostic break a submission
        if remote is None:
            warnings.warn(
                f"remoteexec: could not import cloudpickle in the remote env "
                f"({env_label(use_env) or type(use_env).__name__}); function jobs will "
                f"fail to unpickle. Install cloudpickle there (local has {local[1]}).",
                EnvParityWarning, stacklevel=4)
            return
        problems = env_problems(local, remote)
        if problems:
            warnings.warn(
                "remoteexec: local/remote environment mismatch — " + "; ".join(problems)
                + ". Functions are shipped by value with cloudpickle, so this can cause "
                "unpickling errors on the cluster. Match the Python minor and cloudpickle "
                "major versions, or submit a shell command with cluster.run(...).",
                EnvParityWarning, stacklevel=4)
            return
        cache[key] = time.time()
        state.write_json(cache_path, cache)

    # ---- paths & transfer ----
    def _resolve(self, p: str) -> str:
        p = str(p)
        return p if p[:1] in ("/", "~", "$") else f"{self.project_root}/{p}"

    def make_path(self, *parts, mkdir=True) -> RemotePath:
        """Handle on a durable path relative to the remote project root."""
        self.connect()
        remote = self._resolve("/".join(str(p) for p in parts))
        if mkdir:
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

    # ---- code sync ----
    def _excludes(self) -> list[str]:
        ex = list(DEFAULT_EXCLUDES)
        if self.respect_gitignore:
            ex += syncmod.gitignore_excludes(self.local_dir)
        return ex + self.excludes

    def _cwd_rel(self, sync) -> str:
        """Remote cwd (relative to the project root) mirrors the local cwd
        when the cluster's or this call's sync scope is "cwd"."""
        if "cwd" in (self.sync, syncmod.normalize(sync) if sync is not None else None):
            return syncmod.cwd_rel(self.local_dir, Path.cwd())
        return ""

    def _plan_sync(self, sync) -> _SyncPlan | None:
        scope = self.sync if sync is None else syncmod.normalize(sync)
        if scope == "none":
            return None
        rel = syncmod.cwd_rel(self.local_dir, Path.cwd()) if scope == "cwd" else ""
        excludes = self._excludes()
        sig = syncmod.tree_signature(self.local_dir, scope, rel, excludes)
        key = hashlib.sha1(json.dumps(
            [canonical(self.conn.host), self.project_root, scope, rel]).encode()).hexdigest()[:12]
        return _SyncPlan(scope, rel, excludes, sig, key)

    def _marker(self, plan: _SyncPlan, sig: str | None = None) -> str:
        return f"{self.project_root}/.rx/.sync-{plan.key}-{sig or plan.sig}"

    def _rsync_code(self, plan: _SyncPlan) -> float:
        t0 = time.time()
        if not self._root_made:  # rsync creates only the last path component
            self.conn.run(f"mkdir -p {rq(self.project_root)}")
            self._root_made = True
        self.conn.rsync(f"{self.local_dir}/", self.project_root, up=True,
                        excludes=plan.excludes, extra=syncmod.filter_args(plan.scope, plan.rel))
        return time.time() - t0

    def _synced(self) -> dict:
        return state.read_json(state.home() / "synced.json")

    def _remember_sync(self, plan: _SyncPlan):
        data = self._synced()
        data[plan.key] = plan.sig
        state.write_json(state.home() / "synced.json", data)

    def sync_code(self, sync=None, *, force: bool = False) -> bool:
        """Push the project to the remote workspace now (submissions do this
        automatically). Returns False if the remote was already up to date."""
        self.connect()
        plan = self._plan_sync(sync if sync is not None else (self.sync if self.sync != "none" else True))
        if not force and self._synced().get(plan.key) == plan.sig and self.conn.exists(self._marker(plan)):
            return False
        self._rsync_code(plan)
        rx = f"{self.project_root}/.rx"
        self.conn.run(f"mkdir -p {rq(rx)} && rm -f {rq(rx)}/.sync-{plan.key}-* && touch {rq(self._marker(plan))}")
        self._remember_sync(plan)
        return True

    def _ship(self, rundir: str, files: dict[str, bytes], plan: _SyncPlan | None) -> tuple[str, bool]:
        """Sync code if needed, unpack ``files`` into ``rundir`` and sbatch.
        Returns (sbatch output, whether code was rsynced)."""
        tarball = _tarball(files)
        synced = False
        if plan and self._synced().get(plan.key) != plan.sig:
            self._rsync_code(plan)
            synced = True
        rx = f"{self.project_root}/.rx"
        while True:
            pre = ""
            if plan and synced:
                pre = (f"mkdir -p {rq(rx)} && rm -f {rq(rx)}/.sync-{plan.key}-* "
                       f"&& touch {rq(self._marker(plan))} && ")
            elif plan:  # trust the local cache only if the remote agrees
                pre = f"{{ test -e {rq(self._marker(plan))} || exit {SYNC_MISSING}; }} && "
            r = self.conn.run(
                f"{pre}mkdir -p {rq(rundir)} && cd {rq(rundir)} && tar xzmf - "
                f"&& mkdir -p logs out && sbatch --parsable run.sbatch",
                input=tarball, check=False)
            if r.returncode == SYNC_MISSING and plan and not synced:
                self._rsync_code(plan)
                synced = True
                continue
            break
        if r.returncode != 0:
            raise SubmissionError(f"sbatch failed (rc={r.returncode}): {(r.stderr or r.stdout).strip()}")
        if plan:
            self._remember_sync(plan)
        return r.stdout, synced

    @staticmethod
    def _job_id(sbatch_out: str) -> str:
        lines = [ln for ln in sbatch_out.strip().splitlines() if ln.strip()]
        jid = lines[-1].split(";")[0].strip() if lines else ""
        if not jid.isdigit():
            raise SubmissionError(f"unexpected sbatch output: {sbatch_out!r}")
        return jid

    def _pythonpath(self, cwd: str) -> str:
        """Job cwd, project root, and the remote mirror of each local sys.path
        entry inside the project — so a module imported from a subdirectory
        re-imports by the same name on the cluster."""
        entries = [cwd, self.project_root]
        root = self.local_dir.resolve()
        for p in sys.path:
            if "site-packages" in p:
                continue
            try:
                rel = Path(p or ".").resolve().relative_to(root).as_posix()
            except (ValueError, OSError):
                continue
            entries.append(self.project_root if rel == "." else f"{self.project_root}/{rel}")
        return ":".join(dict.fromkeys(entries))

    # ---- submission ----
    def _prepare(self, name, resources, env, setup, sync):
        self.connect()
        session = new_session_id(name)
        rundir = f"{self.project_root}/.rx/{session}"
        rel = self._cwd_rel(sync)
        cwd = f"{self.project_root}/{rel}" if rel else self.project_root
        eff = self.default_resources.merge(resources)
        eff.job_name = eff.job_name or name
        use_env = env or self.env
        common = dict(env=use_env, setup=[*self.setup, *setup], cd_dir=cwd,
                      logdir=f"{rundir}/logs", pythonpath=self._pythonpath(cwd))
        return session, rundir, cwd, eff, use_env, common

    def _make_job(self, job_id, session, rundir, cwd, name, kind, outdir, result_path=None):
        return Job(self, job_id=job_id, session=session, workdir=cwd, logdir=f"{rundir}/logs",
                   name=name, rundir=rundir, result_path=result_path, outdir=outdir,
                   kind=kind, local_base=str(Path.cwd()))

    def _record(self, job, use_env, eff, **extra):
        state.record(job, env=env_label(use_env), local_dir=str(self.local_dir),
                     partition=eff.partition, **extra)

    def submit(self, fn, args, kwargs, resources, *, env=None, name=None,
               setup=(), sync=None) -> Job:
        name = name or getattr(fn, "__name__", "job")
        session, rundir, cwd, eff, use_env, common = self._prepare(name, resources, env, setup, sync)
        self._maybe_warn_env(env)
        script = render_sbatch(
            eff, runner_path=f"{rundir}/_runner.py", payload_path=f"{rundir}/payload.pkl",
            result_path=f"{rundir}/result.pkl", outdir=f"{rundir}/out", **common)
        files = {"payload.pkl": pack_payload(fn, args, kwargs), **self._runtime_files(),
                 "run.sbatch": script.encode()}
        out, synced = self._ship(rundir, files, self._plan_sync(sync))
        job = self._make_job(self._job_id(out), session, rundir, cwd, eff.job_name, "fn", f"{rundir}/out")
        self._record(job, use_env, eff)
        self._announce(job, synced)
        return job

    def run(self, command: str, *, name=None, env=None, setup=(), sync=None,
            **resources) -> Job:
        """Submit a shell command, e.g. ``cluster.run("python train.py --lr 3e-4",
        partition="ctn", time="01:00:00")``. Runs from the synced project with
        the cluster's env active; nothing is pickled, so no version parity is
        needed. ``job.result()`` waits and raises JobFailed on a non-zero exit."""
        name = name or _name_from_command(command)
        res = resources.pop("_resources", None) or {}
        session, rundir, cwd, eff, use_env, common = self._prepare(
            name, SlurmResources(**{**resources, **res}), env, setup, sync)
        script = render_sbatch(eff, command=wrap_command(use_env, command),
                               outdir=f"{rundir}/out", **common)
        out, synced = self._ship(rundir, {"run.sbatch": script.encode()}, self._plan_sync(sync))
        job = self._make_job(self._job_id(out), session, rundir, cwd, eff.job_name, "cmd", f"{rundir}/out")
        self._record(job, use_env, eff, command=command[:500])
        self._announce(job, synced)
        return job

    def map(self, fn, iterable, *, resources, env=None, name=None,
            setup=(), sync=None, array_throttle=None) -> JobArray:
        items = list(iterable)
        if not items:
            return JobArray([])
        name = name or getattr(fn, "__name__", "job")
        session, rundir, cwd, eff, use_env, common = self._prepare(name, resources, env, setup, sync)
        self._maybe_warn_env(env)
        eff.array = f"0-{len(items) - 1}" + (f"%{array_throttle}" if array_throttle else "")
        script = render_sbatch(eff, runner_path=f"{rundir}/_runner.py", array_dir=rundir, **common)
        files = {}
        for i, item in enumerate(items):
            if isinstance(item, dict):
                a, kw = (), item
            elif isinstance(item, tuple):
                a, kw = item, {}
            else:
                a, kw = (item,), {}
            files[f"payloads/{i}.pkl"] = pack_payload(fn, a, kw)
        files.update(self._runtime_files())
        files["run.sbatch"] = script.encode()
        out, synced = self._ship(rundir, files, self._plan_sync(sync))
        array_id = self._job_id(out)
        jobs = [self._make_job(f"{array_id}_{i}", session, rundir, cwd, f"{eff.job_name}[{i}]",
                               "fn", f"{rundir}/out/{i}", result_path=f"{rundir}/results/{i}.pkl")
                for i in range(len(items))]
        label = env_label(use_env)
        state.record_many({j.job_id: state.make_entry(
            j, env=label, local_dir=str(self.local_dir), partition=eff.partition) for j in jobs})
        self._say(f"submitted '{eff.job_name}' as array {array_id} ({len(items)} tasks) on "
                  f"{self.conn.host}{' — code synced' if synced else ''}")
        return JobArray(jobs)

    def _runtime_files(self) -> dict[str, bytes]:
        return {"_runner.py": Path(_runner.__file__).read_bytes(), **_lib_files()}

    def _announce(self, job: Job, synced: bool):
        self._say(f"submitted '{job.name}' as job {job.job_id} on {self.conn.host}"
                  f"{' — code synced' if synced else ''}  (remoteexec logs {job.job_id} -f)")

    # ---- job control ----
    def status(self, job_id) -> JobState:
        self.connect()
        return _parse_state(self.conn.run(_STATE_CMD.format(id=job_id), check=False).stdout)

    def cancel(self, job_id) -> None:
        self.connect()
        self.conn.run(f"scancel {job_id}", check=False)

    def job(self, job_id) -> Job:
        """Reattach to a job from the local manifest (e.g. after a restart)."""
        rec = state.get(str(job_id))
        if rec is None:
            raise KeyError(f"no manifest entry for job {job_id}")
        return job_from_record(self, str(job_id), rec)

    def jobs(self, limit: int = 20) -> list[Job]:
        """Recent jobs of this cluster submitted from this project, newest first."""
        host = canonical(self.conn.host)
        return [job_from_record(self, jid, rec) for jid, rec in state.recent()
                if rec.get("cluster_host") == host and rec.get("local_dir") == str(self.local_dir)
                ][:limit]

    @classmethod
    def from_config(cls, name=None):
        from ..config import load_cluster
        return load_cluster(cls, name)


def job_from_record(cluster, job_id: str, rec: dict) -> Job:
    return Job(cluster, job_id=job_id, session=rec["session"], workdir=rec["workdir"],
               logdir=rec["logdir"], name=rec["name"], rundir=rec.get("rundir"),
               outdir=rec.get("outdir"), kind=rec.get("kind", "fn"),
               result_path=_array_result(rec, job_id),
               local_base=rec.get("cwd") or rec.get("local_dir"))


def _array_result(rec, job_id):
    if "_" in job_id and rec.get("rundir"):
        return f"{rec['rundir']}/results/{job_id.rsplit('_', 1)[1]}.pkl"
    return None
