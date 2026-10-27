"""Serve the dashboard against a fake local cluster with a few sample jobs.

For working on the web UI (or the macOS app) without a cluster:

    python tests/demo_dashboard.py [--port 8765] [--dir /tmp/rx-demo]

Then open http://127.0.0.1:8765/ (the menu bar app reuses a server on its port).
Uses the same fake Slurm and local-shell transport as the test suite.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent), str(HERE)]

TRAIN = '''import json, os, sys, time
out = os.environ["REMOTEEXEC_OUTPUT_DIR"]
os.makedirs(out + "/plots", exist_ok=True)
for i in range(int(sys.argv[1])):
    print(f"epoch {i}: loss={1 / (i + 1):.4f}", flush=True)
    time.sleep(1)
json.dump({"best_loss": 0.1, "epochs": int(sys.argv[1])}, open(out + "/metrics.json", "w"), indent=2)
open(out + "/plots/loss.svg", "w").write(
    '<svg xmlns="http://www.w3.org/2000/svg" width="240" height="120"><polyline fill="none" '
    'stroke="#d97757" stroke-width="3" points="0,10 60,60 120,85 180,100 240,105"/></svg>')
open(out + "/notes.txt", "w").write("all good\\n")
'''


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--dir", default=None, help="scratch directory (default: a temp dir)")
    a = ap.parse_args()

    base = Path(a.dir or tempfile.mkdtemp(prefix="rx-demo-")).resolve()
    base.mkdir(parents=True, exist_ok=True)
    os.environ["REMOTEEXEC_HOME"] = str(base / "rxhome")
    os.environ["FAKESLURM_DIR"] = str(base / "slurm")
    bindir = base / "bin"
    bindir.mkdir(exist_ok=True)
    for cmd in ("sbatch", "sacct", "squeue", "scancel"):
        f = bindir / cmd
        f.write_text(f'#!/bin/bash\nexec {sys.executable} {HERE / "fakeslurm.py"} {cmd} "$@"\n')
        f.chmod(0o755)
    py = bindir / "python"
    py.write_text(f'#!/bin/bash\nexec {sys.executable} "$@"\n')
    py.chmod(0o755)
    os.environ["PATH"] = f"{bindir}:{os.environ['PATH']}"

    proj = base / "myproject"
    proj.mkdir(exist_ok=True)
    (proj / "pyproject.toml").write_text("[project]\nname = 'myproject'\n")
    (proj / "train.py").write_text(TRAIN)
    os.chdir(proj)

    from conftest import make_cluster
    from remoteexec import hosts
    from remoteexec.gui.server import create_server
    from remoteexec.gui.service import JobService

    cluster = make_cluster(base)
    hosts.set_hosts("fake", ["fake", "fake-remote"])
    cluster.run("python train.py 3", name="train-small", partition="ctn")
    cluster.run("python train.py 3600", name="train-long", partition="gpu")
    cluster.run("python -c 'raise SystemExit(2)'", name="broken")

    @cluster.function(name="sweep")
    def sq(x):
        print("computing", x)
        return x * x

    sq.map([1, 2])
    time.sleep(5)

    class Service(JobService):
        def _cluster(self, host):
            c = make_cluster(base)
            c.connect()
            return c

    server = create_server("127.0.0.1", a.port, Service())
    print(f"demo dashboard on http://127.0.0.1:{a.port}/  (data in {base})", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
