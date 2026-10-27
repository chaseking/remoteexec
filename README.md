# remoteexec

Edit code locally, execute it on a remote Slurm cluster. Implements
[`SPEC_IMPL.md`](SPEC_IMPL.md) — a conda-first, ssh/rsync transport over Slurm,
with a batteries-included `AxonCluster` preset.

```python
from remoteexec import is_remote
from remoteexec.clusters.presets import AxonCluster

cluster = AxonCluster()                       # ssh alias `axon`, default conda env `base`

@cluster.function(partition="ctn", time="00:05:00", mem="1G", name="countdown")
def countdown(seconds=10):
    import time
    for i in range(seconds, 0, -1):
        print(f"  {i}...", flush=True)
        time.sleep(1)
    return {"counted": seconds, "where": "cluster" if is_remote() else "local"}

job = countdown(seconds=10)   # syncs repo, submits via sbatch, returns a Job
job.stream()                  # live-tail the combined log until the job ends
print(job.result())           # -> {'counted': 10, 'where': 'cluster'}
```

## GUI

A local web GUI monitors your jobs in a Claude-Code-style dark dashboard:

```bash
pip install 'remoteexec[gui]'      # fastapi + uvicorn
remoteexec gui                     # serves http://127.0.0.1:8765 and opens a browser
# options: --port 8765  --host 127.0.0.1  --no-browser
```

- **Jobs table** — status pill, job name, project path, started date, running
  time (`[HH:]MM:SS`), environment, job id, host, partition. A filter dropdown
  (checkbox multi-select) toggles **Running** and **Recently finished
  (6h / 1d / 7d)**; the table refreshes every ~3s via one batched `sacct`/`squeue`
  call per host.
- **Click a row** → a live, scrolling console of the job's combined log (polls
  every ~3s, sticks to the bottom unless you scroll up). An actions menu (`⋯`)
  offers **Cancel job** and **Copy paths** (remote workdir / log / local project).

The server binds `127.0.0.1` only and reuses your existing ssh ControlMaster —
auth stays delegated to `~/.ssh/config`, nothing is exposed.

## Running the axon countdown test

`tests/test_axon_countdown.py` submits a real 10-second countdown to the `axon`
cluster, streams its log, and verifies the returned result. Just run it:

```bash
pytest tests/test_axon_countdown.py -s     # the -s matters — see below
# or
python tests/test_axon_countdown.py
```

The test auto-skips if `axon` is unreachable.

**Pass `-s` to see the live output.** Without `-s` (a.k.a. `--capture=no`),
pytest captures all stdout and only shows it if the test *fails* — so a passing
run looks silent (no "submitted job…", no live log). `-s` lets the submit line
and streamed log through as they happen.

**About "live" streaming on axon.** `job.stream()` re-reads the log from a byte
offset each poll, which is robust over axon's NFS (a long-lived `tail -f` on the
login node silently *drops* the bytes the compute node appends). But the compute
node only flushes its stdout to the shared filesystem on periodic write-back, so
a short job like this 10s countdown tends to show its body all at once near the
end rather than ticking one line per second. Longer jobs stream in with bounded
lag. No output is lost either way.

**Environment parity (§26), handled automatically.** Functions are shipped by
value with `cloudpickle`, so the local interpreter must produce pickles the
remote env can read — matching **Python minor version and cloudpickle major
version**. Axon's default conda env (`base`) is currently Python 3.11 +
cloudpickle 2.2.1. If your local interpreter differs (e.g. Python 3.12 +
cloudpickle 3.x), the test probes the remote env and transparently
re-executes itself under a matching `uv`-provided interpreter, so the plain
commands above just work. `uv` must be on `PATH` for this bootstrap; if it
isn't and the local interpreter is incompatible, the test skips with an
explanation.

### Automatic parity warning

The library itself warns when the local and remote environments diverge. On the
first submission, `SlurmCluster` activates the remote env exactly as the job
will, asks its Python for its version and `cloudpickle.__version__`, and emits an
`EnvParityWarning` if the Python minor or cloudpickle major version differs (or
if `cloudpickle` is missing remotely):

```
EnvParityWarning: remoteexec: local/remote environment mismatch —
Python 3.12 (local) vs 3.11 (remote). Functions are shipped by value with
cloudpickle, so this can cause unpickling errors on the cluster. ...
```

The check runs once per cluster and never blocks submission. Disable it with
`SlurmCluster(..., check_env=False)` (also accepted by `AxonCluster`).

### Notes specific to axon

- **No default partition** — jobs must set one. The test uses `partition="ctn"`
  (the account available to this user). The *conda env* is left at the default.
- **Conda activation in batch shells** — axon's non-interactive `sbatch` shells
  don't define the `conda` shell function, so `AxonCluster` enables the
  `eval "$(conda shell.bash hook)"` init step by default (§10.2).

## Offline unit tests

```bash
uv run --python 3.11 --with pytest --with . pytest tests/test_unit.py -q
```
