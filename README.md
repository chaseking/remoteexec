# remoteexec

Edit code locally, run it on a Slurm cluster. Your project is rsynced to the
cluster (only when it changed), jobs are submitted with `sbatch` over your
existing `ssh` config, and logs, results and output files come back through one
API, a CLI, a web dashboard and a macOS menu bar app. The cluster needs nothing
installed but `cloudpickle` — and not even that for shell commands.

```bash
pip install -e .                    # or: uv tool install -e .   (stdlib only + cloudpickle)
remoteexec skill --install          # teach Claude Code agents to use it (~/.claude/skills)
```

## Quick start

**A shell command** — nothing to write:

```bash
remoteexec run -c axon -p ctn -t 01:00:00 -- python train.py --lr 3e-4   # prints the job id
remoteexec wait last --timeout 300   # state + log tail + output files (exit 0/1/2 = ok/failed/running)
```

**A Python function**:

```python
from remoteexec import AxonCluster, is_remote, output_dir

cluster = AxonCluster()                       # ssh alias `axon`, conda env `base`

@cluster.function(partition="ctn", time="00:05:00", mem="1G")
def countdown(seconds=10):
    import time
    for i in range(seconds, 0, -1):
        print(f"  {i}...")
        time.sleep(1)
    (output_dir() / "done.txt").write_text("liftoff")
    return {"counted": seconds, "where": "cluster" if is_remote() else "local"}

job = countdown(seconds=10)   # syncs, submits, returns a Job immediately
job.stream()                  # live log until the job ends
job.result()                  # {'counted': 10, 'where': 'cluster'}; re-raises remote exceptions
job.outputs()                 # {'done.txt': 7}
job.fetch_outputs()           # -> ./remoteexec-out/countdown-<id>/
```

`countdown.local(3)` runs it here first; `REMOTEEXEC_LOCAL=1 python script.py` runs
every decorated function locally. `countdown.map([5, 10, 20])` submits one Slurm
array (`arr.results()` fetches all results in one round trip). `cluster.run("python
train.py", partition="ctn")` is the Python form of `remoteexec run`.

## Output files

Every job gets a private output directory: `remoteexec.output_dir()` in Python,
`$REMOTEEXEC_OUTPUT_DIR` in shell (array tasks get one each). Write results there and
they never collide with other runs, and you can find them without knowing paths:

| | Python | CLI |
|---|---|---|
| list | `job.outputs()` | `remoteexec outputs <id\|last>` |
| read one | `job.read_output("metrics.json")` | `remoteexec cat <id> metrics.json` |
| download | `job.fetch_outputs()` → `./remoteexec-out/<name>-<id>/` | `remoteexec fetch <id>` |

The dashboard shows the same tree with text/image previews. (Relative paths still
write into the shared workspace as before; `job.fetch("path")` fetches those.)

## Monitoring

```bash
remoteexec ls                 # recent jobs submitted from this directory (-a: all)
remoteexec logs last -n 50    # log tail (-f to stream)
remoteexec result <id>        # a function job's return value
remoteexec cancel <id>
remoteexec gui                # web dashboard on http://127.0.0.1:8765
```

`last` means the newest job submitted from the current directory tree. Jobs are
recorded in `~/.remoteexec/manifest.json`, so `cluster.job(id)` reattaches after a
restart; `remoteexec clean --older-than 30 [--remote] -y` prunes old ones.

## How code gets to the cluster

```
~/remoteexec/<project>/                 mirror of your project root, updated incrementally
~/remoteexec/<project>/.rx/<session>/   one per submission: run.sbatch, payload, logs/, out/
```

A submission is a single ssh round trip (a small tarball piped into `tar x && sbatch`)
plus an rsync only when the local tree changed — a loop of 100 submissions syncs
once. The job runs from the mirrored project root with it on `PYTHONPATH`.

`sync=` (on the cluster, per function, or `--sync` on the CLI) chooses what is synced:
`True` (whole project, default), `False`, a list of root-relative paths, or **`"cwd"`**:
only the project's top-level files plus your current directory, and the job runs from
the mirror of that directory. That is the mode for several agents working in
`parent/dir1`, `parent/dir2`, … against shared `parent/*.py`: they share one remote
copy of `parent`, the shared files are only re-sent when they change, and nobody
touches anyone else's directory.

The remote workspace is shared and updated on each submit, so a job that is still
queued when you edit and resubmit runs the edited code.

## Switching ssh hosts

One cluster, several ssh aliases (`axon` on-site, `axon-remote` off-site):

```bash
remoteexec host use axon axon-remote   # route `axon` through axon-remote
remoteexec host use axon auto          # or: first alias that answers
remoteexec host test axon              # check each alias
```

Everything follows the route — `AxonCluster()`, the CLI, the dashboard and the menu bar
app — and jobs stay listed under `axon` whichever alias submitted them. The dashboard's
*Clusters & hosts* page edits the same settings (`~/.remoteexec/hosts.json`).

## Menu bar app (macOS)

`macos/` holds a small native menu bar app: a dropdown of running and recently finished
jobs, a dashboard window (live logs, output files) on click, a host switcher and
finish notifications. Build it with `macos/build.sh --install`; see
[`macos/README.md`](macos/README.md).

## Configuration

Optional `remoteexec.toml` at the project root (or `~/.config/remoteexec/config.toml`):

```toml
default = "axon"                 # lets you drop `-c axon`

[clusters.axon]
preset = "axon"                  # start from AxonCluster
sync = "cwd"
setup = ["export WANDB_MODE=offline"]
[clusters.axon.env]
kind = "conda"                   # conda | uv | venv | module | apptainer | raw
name = "torch24"
init = true
[clusters.axon.default_resources]
partition = "ctn"
time = "04:00:00"
```

`SlurmCluster.from_config("axon")` or `remoteexec.resolve_cluster("axon")` loads it;
`remoteexec run -c NAME` accepts a config name, a preset (`axon`, `axon-jax-gpu`) or any
ssh host. `REMOTEEXEC_HOME` moves `~/.remoteexec`.

## Environment parity

Function jobs ship code by value with cloudpickle, so local and remote need the same
Python minor and cloudpickle major version (axon's `base`: Python 3.11, cloudpickle 2.2).
The first submission checks this (cached for a few days) and emits an
`EnvParityWarning` on a mismatch. Shell-command jobs pickle nothing and don't care.

## Notes for axon

- No default partition: pass one (`partition="ctn"` / `-p ctn`).
- `AxonCluster` evals `conda shell.bash hook` before `conda activate` (batch shells
  need it).
- Output reaches the shared filesystem on NFS write-back, so short jobs may show their
  log all at once at the end. Python output is unbuffered, so nothing is held back.

## Tests

```bash
uv run --python 3.11 --with pytest --with cloudpickle python -m pytest tests/ -q
pytest tests/test_axon_countdown.py -s     # real axon; skips if unreachable
```

The offline suite runs the real submission path end to end against a fake Slurm
(`tests/fakeslurm.py`) with ssh/rsync replaced by a local shell.

## Changes in 0.3

New: `remoteexec run` / `cluster.run` for shell commands; per-job `output_dir()` with
`outputs()`/`read_output()`/`fetch_outputs()`; `sync="cwd"` and sync skipping;
host routing; `wait`/`ls`/`outputs`/`cat`/`clean`/`host`/`skill` commands; dashboard
file browser and host settings; bare `@cluster.function`; `JobArray` batching.
Fixed: a function that raised now surfaces as `RemoteExecutionError` (was `JobFailed`);
`OUT_OF_MEMORY`/`NODE_FAIL` end `wait()` instead of hanging; functions referencing
`remoteexec` work on clusters without it installed (a copy ships with each job); Python
output is unbuffered so logs stream live. The CLI and GUI no longer need typer, rich,
FastAPI or uvicorn. Existing code and manifests keep working.
