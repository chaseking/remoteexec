---
name: remoteexec
description: Run code on the user's Slurm cluster (e.g. axon) with remoteexec — submit shell commands or Python functions from the local project, then check status, read logs and output files. Use whenever work should run on the cluster/GPUs, or to check on or fetch results of cluster jobs.
---

# remoteexec — run jobs on the Slurm cluster

remoteexec rsyncs the local project to the cluster (only when it changed) and submits
via sbatch. Never ssh in, write sbatch scripts, or copy files by hand.

## Submit a shell command (preferred — no wrapper code to write)

```bash
remoteexec run -c axon -p ctn -t 01:00:00 --mem 8G -- python train.py --lr 3e-4
```
Prints the job id and returns immediately. Options: `--gpus 1`, `--cpus 8`, `-n NAME`,
`--env CONDA_ENV` (axon default: `base`), `--setup "module load cuda"`, `-w` (wait and
report). axon has **no default partition**: always pass `-p` (e.g. `ctn`, `gpu`).
The job runs from the remote mirror of the project root; imports and relative paths
behave as when you run from the root locally.

**Write result files to the job's own output dir**: `$REMOTEEXEC_OUTPUT_DIR` in
shell, `remoteexec.output_dir()` in Python (a Path; locally `./remoteexec-out/...`).
Concurrent jobs never collide there and the commands below find the files.

## Check on jobs (don't block for long)

```bash
remoteexec wait last --timeout 300   # state, last 20 log lines, output files
                                     #   exit 0 = completed, 1 = failed, 2 = still running
remoteexec logs last -n 50           # log tail (stdout+stderr combined)
remoteexec ls                        # recent jobs from this directory, live state
remoteexec outputs last              # files in the job's output dir
remoteexec cat last metrics.json     # print one output file
remoteexec fetch last                # download outputs -> ./remoteexec-out/<name>-<id>/
remoteexec cancel <id>
```
`last` = newest job submitted from the current directory; any job id works too. For
long jobs, poll with `wait --timeout` (repeat while it exits 2) instead of `logs -f`,
which blocks until the job ends.

## Python API (return values, parameter sweeps)

```python
from remoteexec import AxonCluster, output_dir
cluster = AxonCluster()                      # AxonCluster(conda_env="torch") for another env

@cluster.function(partition="ctn", time="00:30:00", mem="8G")   # gpus=1 for a GPU
def train(lr):
    ...                                      # project imports work as locally
    (output_dir() / "metrics.json").write_text("...")
    return {"lr": lr, "loss": 0.1}           # keep return values small; big data -> files

job = train(3e-4)                  # submits, returns a Job at once
job.result(timeout=600)            # waits; re-raises the remote exception if it failed
arr = train.map([1e-4, 3e-4])      # one Slurm array; arr.results(return_exceptions=True)
train.local(3e-4)                  # quick local run first
```
Submit from a short script, then monitor with the CLI (`remoteexec wait <id>`,
`remoteexec result <id>`) rather than keeping Python blocked. `cluster.run("python x.py",
partition="ctn")` is the Python form of `remoteexec run`. `REMOTEEXEC_LOCAL=1 python
script.py` runs every decorated function locally instead.

Function jobs pickle code with cloudpickle: local and remote need the same Python minor
and cloudpickle major version. If you see an `EnvParityWarning`, use `remoteexec run`
(no pickling) instead.

## Several agents sharing one project

Working in `parent/dirN/` while other agents use sibling dirs and everyone imports
`parent/*.py`? Add `--sync cwd` (or `AxonCluster(sync="cwd")`): only the top-level
files of the project plus your own dir are synced, the job runs from `parent/dirN`
remotely, and other agents' dirs are never touched.

## Gotchas

- The remote copy of the code is shared and updated on every submit: a job still
  **queued** when you edit-and-resubmit will run the edited code. Vary experiments via
  arguments, not by editing files that pending jobs import.
- `ssh` to axon fails off-site → `remoteexec host use axon axon-remote` (back on-site:
  `remoteexec host use axon axon`). Everything, including `AxonCluster()`, follows it.
- Big inputs: keep them on the cluster and reference absolute paths; anything in the
  project (minus `.gitignore`d files) is synced.
- `remoteexec skill` prints this file; `remoteexec <command> -h` for all options.
