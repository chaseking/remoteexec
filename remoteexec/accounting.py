"""Batched Slurm accounting/queue queries (sacct + squeue)."""
from __future__ import annotations

from .errors import ConnectionError


def _run(conn, cmd, timeout):
    """Run a query; an ssh failure raises instead of reading as "no jobs"."""
    r = conn.run(cmd, check=False, timeout=timeout)
    if r.returncode == 255:
        msg = getattr(conn, "_unreachable_msg", None)
        raise ConnectionError(msg(r.stderr) if msg else f"ssh failed: {r.stderr.strip()}")
    return r.stdout


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
    task's log file (see ``logtail.log_filename``); it comes along free here
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
    out = _run(conn, f"sacct {selector} --format={fmt} -Pn 2>/dev/null", timeout)
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
        q = _run(conn, f"squeue {sel} -h -o '%i|%T|%M|%S' 2>/dev/null", timeout)
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
