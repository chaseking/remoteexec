"""remoteexec command line (stdlib only). ``remoteexec -h`` for commands.

A JOB argument is a job id or ``last`` (the newest job submitted from the
current directory tree).
"""
from __future__ import annotations

import argparse
import shlex
import sys
from dataclasses import replace
from pathlib import Path

from . import hosts, state
from .accounting import batch_status
from .errors import RemoteExecError
from .job import TERMINAL, JobState

# ---------------------------------------------------------------- helpers ---
_clusters: dict = {}


def _cluster_for_host(host: str):
    from .clusters.slurm import SlurmCluster

    if host not in _clusters:
        _clusters[host] = SlurmCluster(host=host, check_env=False, verbose=False)
    return _clusters[host]


def _record(ref: str | None) -> tuple[str, dict]:
    if ref in (None, "last", "-1"):
        items = state.recent(under=Path.cwd(), limit=1) or state.recent(limit=1)
        if not items:
            raise KeyError("no jobs submitted yet")
        return items[0]
    rec = state.get(ref)
    if rec is None:
        raise KeyError(f"unknown job {ref} (not in {state.home() / 'manifest.json'})")
    return ref, rec


def _job(ref):
    from .clusters.slurm import job_from_record

    jid, rec = _record(ref)
    return job_from_record(_cluster_for_host(rec["cluster_host"]), jid, rec)


def _size(n: int) -> str:
    for unit in ("B", "K", "M", "G"):
        if n < 1024 or unit == "G":
            return f"{n:.0f}{unit}" if unit == "B" or n >= 10 else f"{n:.1f}{unit}"
        n /= 1024
    return str(n)


def _live(items) -> dict:
    """{job_id: sacct/squeue info} for manifest items, one query per host."""
    by_host: dict[str, list[str]] = {}
    for jid, rec in items:
        by_host.setdefault(rec.get("cluster_host", ""), []).append(jid)
    info = {}
    for host, ids in by_host.items():
        try:
            info.update(batch_status(_cluster_for_host(host).conn, ids, timeout=30))
        except RemoteExecError as e:
            print(f"({host}: {e})", file=sys.stderr)
    return info


def _print_outputs(job, limit=12):
    files = job.outputs()
    if not files:
        print("outputs: (none)")
        return
    shown = ", ".join(f"{p} {_size(s)}" for p, s in list(files.items())[:limit])
    more = f", … +{len(files) - limit} more" if len(files) > limit else ""
    print(f"outputs ({len(files)}): {shown}{more}")


def _report(job, timeout, tail) -> int:
    """Wait up to ``timeout`` s, print state, log tail and outputs.
    Exit status: 0 completed, 1 failed/cancelled, 2 still running."""
    st = job.wait(timeout=timeout, poll=5)
    info = batch_status(job.conn, [job.job_id], timeout=30).get(job.job_id, {})
    elapsed = f" after {info['elapsed']}" if info.get("elapsed") else ""
    print(f"{job.job_id} {job.name}: {st.value}{elapsed}")
    if st not in TERMINAL:
        print(f"(still running — check again with: remoteexec wait {job.job_id})")
        return 2
    if tail:
        log = job.logs(tail=tail).rstrip("\n")
        if log:
            print(f"--- log (last {tail} lines) ---\n{log}\n---")
    _print_outputs(job)
    return 0 if st is JobState.COMPLETED else 1


# --------------------------------------------------------------- commands ---
def cmd_run(a):
    from .config import resolve_cluster
    from .environment import CondaEnv

    command = a.command[1:] if a.command[:1] == ["--"] else a.command
    if not command:
        raise ValueError("nothing to run: remoteexec run [options] -- <command ...>")
    command = command[0] if len(command) == 1 else shlex.join(command)
    cluster = resolve_cluster(a.cluster)
    env = None
    if a.env:
        env = replace(cluster.env, name=a.env) if isinstance(cluster.env, CondaEnv) else CondaEnv(a.env, init=True)
    res = {k: v for k, v in dict(partition=a.partition, time=a.time, mem=a.mem,
                                 cpus_per_task=a.cpus, gpus=a.gpus, gres=a.gres,
                                 constraint=a.constraint).items() if v is not None}
    job = cluster.run(command, name=a.name, env=env, setup=a.setup or (),
                      sync=a.sync, **res)
    print(job.job_id)
    if a.follow:
        job.stream()
        return _report(job, None, 0)
    if a.wait:
        return _report(job, a.timeout, a.tail)
    return 0


def cmd_wait(a):
    return _report(_job(a.job), a.timeout, a.tail)


def cmd_ls(a):
    items = state.recent(under=None if a.all else Path.cwd(), limit=a.n)
    if not items:
        print("no jobs" + ("" if a.all else " submitted from here (use -a for all)"))
        return 0
    info = _live(items)
    print(f"{'JOB':<12} {'STATE':<11} {'ELAPSED':>9}  {'SUBMITTED':<11}  NAME")
    for jid, rec in items:
        live = info.get(jid, {})
        st = live.get("state") or "?"
        sub = rec.get("submitted_at", "")[5:16].replace("T", " ")
        print(f"{jid:<12} {st:<11} {live.get('elapsed', ''):>9}  {sub:<11}  {rec.get('name', '')}")
    return 0


def cmd_status(a):
    if not a.jobs:
        a.all, a.n = False, 15
        return cmd_ls(a)
    items = [_record(j) for j in a.jobs]
    info = _live(items)
    for jid, rec in items:
        print(f"{jid}\t{rec.get('name', '')}\t{info.get(jid, {}).get('state') or 'UNKNOWN'}")
    return 0


def cmd_logs(a):
    job = _job(a.job)
    if a.follow:
        job.stream()
        return 0
    n = a.n if a.n is not None else 200
    text = job.logs(tail=n or None)
    sys.stdout.write(text)
    if n and text.count("\n") >= n:
        print(f"(last {n} lines; -n 0 for the whole log)", file=sys.stderr)
    return 0


def cmd_outputs(a):
    job = _job(a.job)
    files = job.outputs()
    for p, s in files.items():
        print(f"{_size(s):>6}  {p}")
    if not files:
        print("(no output files)", file=sys.stderr)
    print(f"({job.cluster.conn.host}:{job.outdir})", file=sys.stderr)
    return 0


def cmd_cat(a):
    sys.stdout.write(_job(a.job).read_output(a.path))
    return 0


def cmd_fetch(a):
    job = _job(a.job)
    print(job.fetch(a.remote, a.local) if a.remote else job.fetch_outputs(a.local))
    return 0


def cmd_result(a):
    import pprint

    pprint.pprint(_job(a.job).result(timeout=a.timeout))
    return 0


def cmd_cancel(a):
    for ref in a.jobs:
        job = _job(ref)
        job.cancel()
        print(f"cancelled {job.job_id}")
    return 0


def cmd_connect(a):
    from .config import resolve_cluster

    c = resolve_cluster(a.cluster)
    c.connect()
    print(f"connected to {c.conn.host} via {c.conn.alias} (remote root {c.remote_root})")
    return 0


def cmd_gui(a):
    from .gui.server import serve

    serve(host=a.host, port=a.port, open_browser=not a.no_browser, parent_pid=a.parent_pid)
    return 0


def cmd_clean(a):
    old = state.prune(a.older_than, dry_run=not a.yes)
    verb = "removed" if a.yes else "would remove"
    print(f"{verb} {len(old)} manifest entries older than {a.older_than:g} days")
    if a.remote and old:
        by_host: dict[str, set] = {}
        for rec in old.values():
            rd = rec.get("rundir") or ""
            if "/.rx/" in rd:  # never anything but a per-run dir
                by_host.setdefault(rec["cluster_host"], set()).add(rd)
        for host, dirs in by_host.items():
            print(f"{verb} {len(dirs)} run dirs on {host}")
            if a.yes:
                conn = _cluster_for_host(host).conn
                dirs = sorted(dirs)
                for i in range(0, len(dirs), 200):
                    conn.run("rm -rf " + " ".join(shlex.quote(d) for d in dirs[i:i + 200]), check=False)
    if not a.yes and old:
        print("(dry run — add -y to do it)")
    return 0


def cmd_host(a):
    if a.action in (None, "ls"):
        clusters = hosts.load()
        if not clusters:
            print("no clusters registered (remoteexec host add NAME ALIAS...)")
        for name, spec in clusters.items():
            print(f"{name}: {' '.join(spec['hosts'])}  (active: {spec['active']})")
        return 0
    if a.action == "add":
        hosts.set_hosts(a.name, [a.name, *a.aliases] if a.name not in a.aliases else a.aliases)
    elif a.action == "use":
        hosts.use(a.name, a.alias)
    elif a.action == "rm":
        hosts.remove(a.name)
    elif a.action == "test":
        from .connection import SSHConnection

        name, aliases, active = hosts.lookup(a.name)
        for alias in ([a.name] if a.name != name else aliases):
            ok, ms, err = SSHConnection(alias).probe(alias)
            print(f"{alias}: {'ok' if ok else 'FAIL'} ({ms:.0f} ms){'  ' + err if err else ''}")
        return 0
    return cmd_host(argparse.Namespace(action="ls"))


def cmd_skill(a):
    src = Path(__file__).parent / "skill" / "SKILL.md"
    if not a.install:
        sys.stdout.write(src.read_text())
        return 0
    base = Path.cwd() / ".claude" if a.project else Path("~/.claude").expanduser()
    dst = base / "skills" / "remoteexec" / "SKILL.md"
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() and dst.read_text() != src.read_text():
        dst.with_suffix(".md.bak").write_text(dst.read_text())
        print(f"(previous version saved as {dst.with_suffix('.md.bak')})")
    dst.write_text(src.read_text())
    print(f"installed {dst}")
    return 0


# ----------------------------------------------------------------- parser ---
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="remoteexec", description="Run jobs on a remote Slurm cluster.")
    sub = p.add_subparsers(metavar="COMMAND")

    def cmd(name, fn, help, **kw):
        sp = sub.add_parser(name, help=help, description=help, **kw)
        sp.set_defaults(func=fn)
        return sp

    r = cmd("run", cmd_run, "submit a shell command from the synced project; prints the job id",
            epilog="example: remoteexec run -c axon -p ctn -t 1:00:00 -- python train.py --lr 3e-4")
    r.add_argument("-c", "--cluster", help="config/preset name or ssh host (default: $REMOTEEXEC_CLUSTER or config default)")
    r.add_argument("-p", "--partition")
    r.add_argument("-t", "--time", help="walltime, e.g. 01:00:00")
    r.add_argument("--mem", help="e.g. 8G")
    r.add_argument("--cpus", type=int, help="cpus per task")
    r.add_argument("--gpus", help="e.g. 1 or a100:2")
    r.add_argument("--gres")
    r.add_argument("--constraint")
    r.add_argument("-n", "--name", help="job name (default: script name)")
    r.add_argument("--env", help="conda env to activate (default: the cluster's)")
    r.add_argument("--setup", action="append", help="shell line to run first (repeatable)")
    r.add_argument("--sync", choices=["all", "cwd", "none"], help="what code to sync (default: cluster's)")
    g = r.add_mutually_exclusive_group()
    g.add_argument("-f", "--follow", action="store_true", help="stream the log until the job ends")
    g.add_argument("-w", "--wait", action="store_true", help="wait, then report like `remoteexec wait`")
    r.add_argument("--timeout", type=float, help="with --wait: give up after SECONDS (exit 2)")
    r.add_argument("--tail", type=int, default=20, help="log lines shown by --wait (default 20)")
    r.add_argument("command", nargs=argparse.REMAINDER)

    w = cmd("wait", cmd_wait, "wait for a job, then print state, log tail and outputs "
            "(exit 0 completed / 1 failed / 2 still running)")
    w.add_argument("job", nargs="?", default="last")
    w.add_argument("--timeout", type=float, help="give up after SECONDS (exit 2)")
    w.add_argument("--tail", type=int, default=20, help="log lines to show (default 20)")

    ls = cmd("ls", cmd_ls, "recent jobs submitted from this directory tree, with live state")
    ls.add_argument("-a", "--all", action="store_true", help="jobs from anywhere")
    ls.add_argument("-n", type=int, default=15, help="how many (default 15)")

    s = cmd("status", cmd_status, "state of the given jobs (no args: like ls)")
    s.add_argument("jobs", nargs="*")

    lg = cmd("logs", cmd_logs, "print a job's combined stdout/stderr log")
    lg.add_argument("job", nargs="?", default="last")
    lg.add_argument("-f", "--follow", action="store_true", help="keep streaming until the job ends")
    lg.add_argument("-n", type=int, help="last N lines (default 200; 0 = all)")

    o = cmd("outputs", cmd_outputs, "list the files a job wrote to its output_dir()")
    o.add_argument("job", nargs="?", default="last")

    ct = cmd("cat", cmd_cat, "print one output file")
    ct.add_argument("job")
    ct.add_argument("path", help="relative to the job's output dir")

    f = cmd("fetch", cmd_fetch, "download a job's outputs (default ./remoteexec-out/<name>-<id>), "
            "or REMOTE relative to the job's remote cwd")
    f.add_argument("job")
    f.add_argument("remote", nargs="?")
    f.add_argument("local", nargs="?")

    rs = cmd("result", cmd_result, "print a function job's return value")
    rs.add_argument("job", nargs="?", default="last")
    rs.add_argument("--timeout", type=float)

    cn = cmd("cancel", cmd_cancel, "cancel jobs")
    cn.add_argument("jobs", nargs="+")

    co = cmd("connect", cmd_connect, "open (and keep) the ssh connection")
    co.add_argument("cluster", nargs="?")

    gu = cmd("gui", cmd_gui, "local web dashboard")
    gu.add_argument("--port", type=int, default=8765)
    gu.add_argument("--host", default="127.0.0.1")
    gu.add_argument("--no-browser", action="store_true")
    gu.add_argument("--parent-pid", type=int, help=argparse.SUPPRESS)

    cl = cmd("clean", cmd_clean, "forget old jobs (and with --remote delete their run dirs)")
    cl.add_argument("--older-than", type=float, default=30, metavar="DAYS")
    cl.add_argument("--remote", action="store_true", help="also rm -rf their .rx run dirs")
    cl.add_argument("-y", "--yes", action="store_true", help="actually do it")

    h = cmd("host", cmd_host, "route a cluster through one of its ssh aliases (e.g. axon via axon-remote)",
            epilog="remoteexec host add axon axon-remote   # then: remoteexec host use axon axon-remote|axon|auto")
    hs = h.add_subparsers(dest="action", metavar="ACTION")
    hs.add_parser("ls", help="list clusters and their aliases")
    ha = hs.add_parser("add", help="register NAME with ssh aliases")
    ha.add_argument("name")
    ha.add_argument("aliases", nargs="+")
    hu = hs.add_parser("use", help="route NAME through ALIAS (or 'auto': first that answers)")
    hu.add_argument("name")
    hu.add_argument("alias")
    hr = hs.add_parser("rm", help="forget NAME")
    hr.add_argument("name")
    ht = hs.add_parser("test", help="check that each alias of NAME answers")
    ht.add_argument("name")

    sk = cmd("skill", cmd_skill, "print the agent skill file (or install it for Claude Code)")
    sk.add_argument("--install", action="store_true", help="copy to ~/.claude/skills/remoteexec/")
    sk.add_argument("--project", action="store_true", help="with --install: into ./.claude/skills/ instead")
    return p


def main(argv=None):
    p = build_parser()
    a = p.parse_args(argv)
    if not getattr(a, "func", None):
        p.print_help()
        sys.exit(0)
    try:
        rc = a.func(a) or 0
    except KeyboardInterrupt:
        rc = 130
    except (RemoteExecError, KeyError, ValueError, FileNotFoundError) as e:
        msg = e.args[0] if isinstance(e, KeyError) and e.args else e
        print(f"remoteexec: {msg}", file=sys.stderr)
        rc = 1
    sys.exit(rc)


app = main  # entry point name kept from earlier versions

if __name__ == "__main__":
    main()
