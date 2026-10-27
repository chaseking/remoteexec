"""End-to-end against a fake Slurm on this machine (see conftest/fakeslurm).

Exercises the real submission path — tar bundle over the transport, sbatch
script, runner, result/outputs retrieval, code-sync skipping — with ssh and
rsync swapped for a local shell. Functions are defined inside the tests so
cloudpickle ships them by value, as it would from a notebook.
"""
from __future__ import annotations

import os
import time

import pytest

from conftest import make_cluster
from remoteexec import JobFailed, JobState, RemoteExecutionError, state


def test_function_result_logs_and_outputs(fake_slurm, project, tmp_path):
    c = make_cluster(tmp_path)

    @c.function(name="hello", partition="ctn")
    def hello(x):
        import os

        from remoteexec import is_remote, output_dir

        (output_dir() / "sub").mkdir()
        (output_dir() / "sub" / "metrics.json").write_text('{"x": %d}' % x)
        print("cwd", os.getcwd())
        return {"x": x, "remote": is_remote()}

    job = hello(21)
    assert job.result(timeout=60) == {"x": 21, "remote": True}
    assert job.state() is JobState.COMPLETED
    assert job.outputs() == {"sub/metrics.json": 9}
    assert job.read_output("sub/metrics.json") == '{"x": 21}'
    log = job.logs()
    assert f"cwd {c.project_root}\n" in log and "rc=0" in log

    local = job.fetch_outputs()
    assert local == project / "remoteexec-out" / f"hello-{job.job_id}"
    assert (local / "sub" / "metrics.json").read_text() == '{"x": 21}'

    # reattach from the manifest, as a restarted notebook / the CLI would
    again = c.job(job.job_id)
    assert again.result() == {"x": 21, "remote": True}
    assert again.outputs() == {"sub/metrics.json": 9}
    rec = state.get(job.job_id)
    assert rec["kind"] == "fn" and rec["partition"] == "ctn" and rec["cwd"] == str(project)


def test_function_exception_surfaces_as_remote_error(fake_slurm, project, tmp_path):
    c = make_cluster(tmp_path)

    @c.function
    def boom():
        raise ValueError("nope")

    job = boom()
    with pytest.raises(RemoteExecutionError) as ei:
        job.result(timeout=60)
    assert ei.value.exc_type == "ValueError" and "nope" in ei.value.remote_traceback
    assert job.state() is JobState.FAILED


def test_shell_command_job(fake_slurm, project, tmp_path):
    c = make_cluster(tmp_path)
    (project / "dir1" / "go.py").write_text(
        "import os, sys, api\n"
        "open(os.environ['REMOTEEXEC_OUTPUT_DIR'] + '/out.txt', 'w').write(str(api.double(21)))\n"
        "print('args', sys.argv[1:])\n")
    job = c.run("python dir1/go.py --lr 3e-4", partition="ctn")
    assert job.name == "go" and job.kind == "cmd"
    assert job.result(timeout=60) is None
    assert job.read_output("out.txt") == "42"
    assert "args ['--lr', '3e-4']" in job.logs()

    bad = c.run("exit 3", name="bad")
    with pytest.raises(JobFailed):
        bad.result(timeout=60)
    assert "rc=3" not in bad.logs()  # `exit` ends the script; Slurm reports it


def test_map_runs_one_array_with_per_task_outputs(fake_slurm, project, tmp_path):
    c = make_cluster(tmp_path)

    @c.function
    def sq(x):
        from remoteexec import output_dir

        (output_dir() / "v.txt").write_text(str(x * x))
        if x == 3:
            raise RuntimeError("three")
        return x * x

    arr = sq.map([1, 2, 3])
    assert len(arr) == 3 and arr[0].job_id.endswith("_0")
    res = arr.results(return_exceptions=True, timeout=60)
    assert res[:2] == [1, 4] and isinstance(res[2], RemoteExecutionError)
    assert arr.states() == [JobState.COMPLETED, JobState.COMPLETED, JobState.FAILED]
    assert arr[1].read_output("v.txt") == "4"
    with pytest.raises(RemoteExecutionError):
        arr.results()
    # array tasks reattach with the right result file
    assert c.job(arr[1].job_id).result() == 4


def test_unchanged_code_is_not_resynced(fake_slurm, project, tmp_path, monkeypatch):
    c = make_cluster(tmp_path)
    calls = []
    real = c.conn.rsync
    monkeypatch.setattr(c.conn, "rsync", lambda *a, **k: (calls.append(a), real(*a, **k))[1])

    c.run("true").result(timeout=60)
    assert len(calls) == 1
    c.run("true").result(timeout=60)
    assert len(calls) == 1  # same tree: skipped

    (project / "api.py").write_text("def double(x):\n    return x + x\n")
    c.run("true").result(timeout=60)
    assert len(calls) == 2  # edited: synced again

    # the remote lost its copy (e.g. someone cleaned it): the marker guard notices
    for m in (tmp_path / "remote" / "parent" / ".rx").glob(".sync-*"):
        m.unlink()
    c.run("true").result(timeout=60)
    assert len(calls) == 3


def test_sync_cwd_shares_parent_and_ignores_siblings(fake_slurm, project, tmp_path, monkeypatch):
    """Agents in parent/dir1 and parent/dir2 share parent/*.py remotely; each
    syncs only its own dir and runs from it."""
    remote = tmp_path / "remote" / "parent"
    monkeypatch.chdir(project / "dir1")
    c1 = make_cluster(tmp_path, sync="cwd")
    assert c1.local_dir == project

    @c1.function
    def where():
        import os

        import api
        import task

        return os.getcwd(), task.WHO, api.double(2)

    assert where().result(timeout=60) == (str(remote / "dir1"), "dir1", 4)
    assert (remote / "api.py").exists() and (remote / "dir1" / "task.py").exists()
    assert not (remote / "dir2").exists()

    monkeypatch.chdir(project / "dir2")
    c2 = make_cluster(tmp_path, sync="cwd")
    job = c2.run("python -c 'import task, os; print(task.WHO, os.getcwd())'")
    job.result(timeout=60)
    assert f"dir2 {remote / 'dir2'}" in job.logs()
    assert (remote / "dir1" / "task.py").exists()  # dir1's copy untouched


def test_cancel(fake_slurm, project, tmp_path):
    c = make_cluster(tmp_path)
    job = c.run("sleep 30", name="sleeper")
    deadline = time.time() + 10
    while job.state() is not JobState.RUNNING and time.time() < deadline:
        time.sleep(0.2)
    job.cancel()
    assert job.wait(timeout=20) is JobState.CANCELLED
    with pytest.raises(JobFailed):
        job.result()


def test_run_local_and_env_switch(fake_slurm, project, tmp_path, monkeypatch):
    c = make_cluster(tmp_path)

    @c.function
    def where():
        from remoteexec import is_remote, output_dir

        (output_dir() / "f").write_text("x")
        return is_remote()

    monkeypatch.setenv("REMOTEEXEC_LOCAL", "1")
    job = where()
    assert job.result() is False
    assert job.outputs() == {"f": 1}
    assert os.path.isdir(project / "remoteexec-out" / "where-local")
