"""A tiny stand-in for Slurm's CLI, enough to run remoteexec end to end.

``python fakeslurm.py <sbatch|sacct|squeue|scancel> ...``; state lives in
``$FAKESLURM_DIR``. Jobs run immediately as detached local processes.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

D = Path(os.environ.get("FAKESLURM_DIR", "/tmp/fakeslurm"))
JOBS = D / "jobs"


def _now():
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _next_id() -> int:
    D.mkdir(parents=True, exist_ok=True)
    with open(D / "counter", "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        fh.seek(0)
        n = int(fh.read().strip() or 1000) + 1
        fh.seek(0)
        fh.truncate()
        fh.write(str(n))
    return n


def _save(jid, rec):
    JOBS.mkdir(parents=True, exist_ok=True)
    tmp = JOBS / f".{jid}.{os.getpid()}"
    tmp.write_text(json.dumps(rec))
    os.replace(tmp, JOBS / f"{jid}.json")


def _load(jid):
    try:
        return json.loads((JOBS / f"{jid}.json").read_text())
    except (OSError, ValueError):
        return None


def _all():
    return {p.stem: _load(p.stem) for p in sorted(JOBS.glob("*.json"))} if JOBS.exists() else {}


def sbatch(args):
    script = Path([a for a in args if not a.startswith("--")][-1]).resolve()
    text = script.read_text()
    directives = dict(re.findall(r"^#SBATCH --([\w-]+)=(.*)$", text, re.M))
    base = _next_id()
    if "array" in directives:
        lo, hi = directives["array"].split("%")[0].split("-")
        tasks = [(f"{base}_{i}", i, _next_id()) for i in range(int(lo), int(hi) + 1)]
    else:
        tasks = [(str(base), None, base)]
    for jid, task, raw in tasks:
        out = directives["output"].replace("%j", str(raw))
        env = {**os.environ, "SLURM_JOB_ID": str(raw)}
        if task is not None:
            env["SLURM_ARRAY_TASK_ID"] = str(task)
        _save(jid, {"state": "RUNNING", "raw": str(raw), "start": _now(), "end": "",
                    "exitcode": "", "name": directives.get("job-name", "")})
        finish = f"{sys.executable} {Path(__file__).resolve()} _finish {jid} $rc"
        p = subprocess.Popen(
            ["bash", "-c", f'bash {script} > {out} 2>&1; rc=$?; {finish}'],
            cwd=os.getcwd(), env=env, start_new_session=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        rec = _load(jid)
        rec["pid"] = p.pid
        _save(jid, rec)
    print(base)


def _finish(jid, rc):
    rec = _load(jid)
    if rec["state"] != "CANCELLED":
        rec["state"] = "COMPLETED" if int(rc) == 0 else "FAILED"
    rec["end"], rec["exitcode"] = _now(), f"{rc}:0"
    _save(jid, rec)


def _elapsed(rec):
    start = datetime.strptime(rec["start"], "%Y-%m-%dT%H:%M:%S")
    end = datetime.strptime(rec["end"], "%Y-%m-%dT%H:%M:%S") if rec["end"] else datetime.now()
    s = int((end - start).total_seconds())
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def _opt(args, flag):
    return args[args.index(flag) + 1] if flag in args else None


def sacct(args):
    fmt = (_opt(args, "--format") or next((a.split("=", 1)[1] for a in args
                                           if a.startswith("--format=")), "State")).split(",")
    ids = _opt(args, "-j")
    jobs = _all()
    sel = ids.split(",") if ids else list(jobs)
    for jid in sel:
        rec = jobs.get(jid)
        if rec is None:
            continue
        row = {"JobID": jid, "State": rec["state"], "Start": rec["start"], "End": rec["end"] or "Unknown",
               "Elapsed": _elapsed(rec), "ExitCode": rec["exitcode"] or "0:0", "JobIDRaw": rec["raw"]}
        print("|".join(row[f] for f in fmt))
        step = {**row, "JobID": f"{jid}.batch", "JobIDRaw": f"{rec['raw']}.batch"}
        print("|".join(step[f] for f in fmt))


def squeue(args):
    fmt = _opt(args, "-o") or "%i|%T"
    ids = _opt(args, "-j")
    jobs = _all()
    for jid in (ids.split(",") if ids else list(jobs)):
        rec = jobs.get(jid)
        if not rec or rec["state"] not in ("RUNNING", "PENDING"):
            continue
        print(fmt.replace("%i", jid).replace("%T", rec["state"])
              .replace("%M", _elapsed(rec)).replace("%S", rec["start"]))


def scancel(args):
    target = args[-1]
    for jid, rec in _all().items():
        if (jid == target or jid.split("_")[0] == target) and rec["state"] == "RUNNING":
            rec["state"] = "CANCELLED"
            _save(jid, rec)
            try:
                os.killpg(rec["pid"], signal.SIGTERM)
            except (OSError, KeyError):
                pass


if __name__ == "__main__":
    cmd, rest = sys.argv[1], sys.argv[2:]
    if cmd == "_finish":
        _finish(*rest)
    else:
        {"sbatch": sbatch, "sacct": sacct, "squeue": squeue, "scancel": scancel}[cmd](rest)
