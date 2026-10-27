"""Integration test: run a simple 10-second countdown on the `axon` cluster.

Uses the AxonCluster preset with the *default* conda environment (`base`) — no
user/password (ssh-config alias `axon`), no env specified. Submits a job that
counts down from 10 to 1 (one second each), streams the live log, then pulls the
result back and verifies it.

Environment parity: functions are shipped by value with ``cloudpickle``,
so the local interpreter must produce pickles the remote env can read. Axon's
default conda env (`base`) is currently Python 3.11 + cloudpickle 2.2.1. If the
local interpreter doesn't match, this test transparently re-executes itself
under a matching `uv`-provided interpreter, so a plain ``pytest`` just works.

Run it any of these ways:

    pytest tests/test_axon_countdown.py -s
    python tests/test_axon_countdown.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

import pytest

# axon has no default Slurm partition; submit under the partition we can use.
PARTITION = "ctn"
_REEXEC_FLAG = "REMOTEEXEC_TEST_REEXEC"


# --------------------------------------------------------------------------- #
# Environment probing / parity
# --------------------------------------------------------------------------- #
def _ssh(*args, timeout=30):
    return subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "axon", *args],
        capture_output=True, text=True, timeout=timeout,
    )


def axon_reachable() -> bool:
    if shutil.which("ssh") is None:
        return False
    try:
        return _ssh("true").returncode == 0
    except Exception:
        return False


def remote_env_info():
    """(python_minor, cloudpickle_version) of axon's default conda env (`base`)."""
    out = _ssh(
        "python -c "
        "'import sys,cloudpickle;"
        'print(f"{sys.version_info.major}.{sys.version_info.minor}",'
        "cloudpickle.__version__)'"
    ).stdout
    # the login shell prints a CUDA banner to stderr; the answer is the last line
    line = [ln for ln in out.splitlines() if ln.strip()][-1]
    py, cp = line.split()
    return py, cp


def _local_info():
    import cloudpickle

    return (
        f"{sys.version_info.major}.{sys.version_info.minor}",
        cloudpickle.__version__,
    )


def _compatible(local, remote) -> bool:
    """By-value cloudpickle needs matching Python minor *and* cloudpickle major."""
    lpy, lcp = local
    rpy, rcp = remote
    return lpy == rpy and lcp.split(".")[0] == rcp.split(".")[0]


# --------------------------------------------------------------------------- #
# The actual run (executed under a remote-compatible interpreter)
# --------------------------------------------------------------------------- #
def run_countdown():
    from remoteexec import is_remote
    from remoteexec.clusters.presets import AxonCluster

    # Default conda environment (base) — nothing to specify.
    cluster = AxonCluster()

    @cluster.function(partition=PARTITION, time="00:05:00", mem="1G",
                      cpus_per_task=1, name="countdown")
    def countdown(seconds: int = 10):
        import time

        where = "cluster" if is_remote() else "local"
        print(f"countdown starting on {where}: {seconds}s", flush=True)
        for i in range(seconds, 0, -1):
            print(f"  {i}...", flush=True)
            time.sleep(1)
        print("liftoff!", flush=True)
        return {"counted": seconds, "where": where}

    job = countdown(seconds=10)
    print(f"\nsubmitted job {job.job_id} ({job.name})", flush=True)
    print(f"log: {job.logfile}\n", flush=True)
    print("--- live log ---", flush=True)
    job.stream()  # live tail of the combined log until the job finishes
    print("\n--- result ---", flush=True)
    result = job.result(timeout=300)  # pull result.pkl, unpickle, re-raise on error
    print(result, flush=True)
    assert result == {"counted": 10, "where": "cluster"}, result
    return result


def _reexec_under_matching_interpreter() -> int:
    """Re-run *this file* as a script under a uv-provided interpreter matching
    axon's default env, so by-value pickling round-trips cleanly."""
    rpy, rcp = remote_env_info()
    env = {**os.environ, _REEXEC_FLAG: "1", "PYTHONUNBUFFERED": "1"}
    cmd = [
        "uv", "run", "--python", rpy,
        "--with", f"cloudpickle=={rcp}", "--with", "pytest",
        "python", "-u", os.path.abspath(__file__),
    ]
    print(f"[test] local env {_local_info()} != axon {(rpy, rcp)}; "
          f"re-executing under uv python {rpy} + cloudpickle {rcp}", flush=True)
    return subprocess.call(cmd, env=env)


# --------------------------------------------------------------------------- #
# pytest entrypoint
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not axon_reachable(), reason="axon cluster not reachable")
def test_axon_countdown():
    already_reexec = os.environ.get(_REEXEC_FLAG) == "1"
    if not already_reexec and not _compatible(_local_info(), remote_env_info()):
        if shutil.which("uv") is None:
            pytest.skip(
                "local interpreter is incompatible with axon's default env "
                "(needs matching Python minor + cloudpickle major) and `uv` is "
                "not available to bootstrap a matching one"
            )
        rc = _reexec_under_matching_interpreter()
        assert rc == 0, f"re-executed run failed (rc={rc})"
        return
    run_countdown()


@pytest.mark.skipif(not axon_reachable(), reason="axon cluster not reachable")
def test_axon_command_and_outputs():
    """Shell-command job (nothing pickled: no parity needed) writing an output file."""
    from remoteexec.clusters.presets import AxonCluster

    cluster = AxonCluster(check_env=False)
    job = cluster.run(
        "echo hello && python -c 'import platform; print(platform.python_version())' "
        '> "$REMOTEEXEC_OUTPUT_DIR/py.txt"',
        partition=PARTITION, time="00:05:00", mem="1G", cpus_per_task=1, name="rx-cmd-test")
    print(f"\nsubmitted job {job.job_id}", flush=True)
    job.result(timeout=300)
    assert "hello" in job.logs()
    assert list(job.outputs()) == ["py.txt"]
    assert job.read_output("py.txt").startswith("3.")


if __name__ == "__main__":
    if not axon_reachable():
        print("axon not reachable — cannot run the integration test")
        sys.exit(1)
    # If invoked directly under an incompatible interpreter, bootstrap too.
    if os.environ.get(_REEXEC_FLAG) != "1" and not _compatible(
        _local_info(), remote_env_info()
    ):
        if shutil.which("uv") is None:
            print("incompatible local interpreter and no `uv` to bootstrap")
            sys.exit(1)
        sys.exit(_reexec_under_matching_interpreter())
    run_countdown()
    print("\nPASS: countdown ran on axon and returned the expected result.")
