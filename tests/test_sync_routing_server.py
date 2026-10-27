"""Offline unit tests: sync scoping, host routing, templates, the HTTP server."""
from __future__ import annotations

import json
import shutil
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from remoteexec import connection, hosts
from remoteexec import sync as syncmod
from remoteexec.connection import SSHConnection
from remoteexec.environment import ApptainerEnv, CondaEnv, UvEnv, wrap_command
from remoteexec.paths import safe_relpath
from remoteexec.resources import SlurmResources
from remoteexec.templates import render_sbatch


# ---- sync scopes ----
def test_filter_args_cwd_scope():
    assert syncmod.filter_args("cwd", "") == []
    assert syncmod.filter_args("cwd", "dir1") == [
        "--include", "/dir1/", "--include", "/dir1/**", "--exclude", "/*/"]
    assert syncmod.filter_args("cwd", "a/b") == [
        "--include", "/a/", "--include", "/a/b/", "--include", "/a/b/**",
        "--exclude", "/a/*/", "--exclude", "/*/"]


def test_filter_args_list_scope():
    args = syncmod.filter_args(["*.py", "src/pkg/"])
    assert args[-2:] == ["--exclude", "*"]
    assert args[1:-2:2] == ["/*.py", "/*.py/**", "/src/", "/src/pkg", "/src/pkg/**"]


def test_normalize():
    assert syncmod.normalize(True) == syncmod.normalize(None) == "all"
    assert syncmod.normalize(False) == "none"
    assert syncmod.normalize("src") == ["src"]


def test_tree_signature_tracks_only_what_the_scope_syncs(tmp_path):
    (tmp_path / "dir1").mkdir()
    (tmp_path / "dir2").mkdir()
    (tmp_path / "api.py").write_text("a")
    (tmp_path / "dir1" / "x.py").write_text("x")
    (tmp_path / "dir2" / "y.py").write_text("y")
    ex = syncmod.DEFAULT_EXCLUDES

    def sig():
        return syncmod.tree_signature(tmp_path, "cwd", "dir1", ex)

    s0 = sig()
    (tmp_path / "dir2" / "y.py").write_text("changed")
    assert sig() == s0  # sibling edits don't matter
    (tmp_path / "api.py").write_text("changed")
    s1 = sig()
    assert s1 != s0  # shared parent files do
    (tmp_path / "dir1" / "__pycache__").mkdir()
    (tmp_path / "dir1" / "__pycache__" / "x.pyc").write_text("junk")
    assert sig() == s1  # excluded dirs are pruned
    full = syncmod.tree_signature(tmp_path, "all", "", ex)
    (tmp_path / "dir2" / "y.py").write_text("again")
    assert syncmod.tree_signature(tmp_path, "all", "", ex) != full


def test_cwd_rel(tmp_path):
    (tmp_path / "a" / "b").mkdir(parents=True)
    assert syncmod.cwd_rel(tmp_path, tmp_path / "a" / "b") == "a/b"
    assert syncmod.cwd_rel(tmp_path, tmp_path) == ""
    assert syncmod.cwd_rel(tmp_path / "a", tmp_path) == ""


# ---- host routing ----
def test_routing(rx_home, monkeypatch):
    assert hosts.lookup("axon") == ("axon", ["axon"], "axon")
    hosts.set_hosts("axon", ["axon", "axon-remote"])
    assert SSHConnection("axon").alias == "axon"
    hosts.use("axon", "axon-remote")
    assert SSHConnection("axon").alias == "axon-remote"
    assert hosts.canonical("axon-remote") == "axon"
    # an explicit alias is honored, but still recorded under the cluster name
    assert SSHConnection("axon-remote").alias == "axon-remote"

    hosts.use("axon", "auto")
    connection._auto.clear()
    probed = []

    def fake_probe(self, alias=None, timeout=8):
        probed.append(alias)
        return alias == "axon-remote", 1.0, ""

    monkeypatch.setattr(SSHConnection, "probe", fake_probe)
    assert SSHConnection("axon").alias == "axon-remote"
    assert probed == ["axon", "axon-remote"]
    assert SSHConnection("axon").alias == "axon-remote" and len(probed) == 2  # cached
    assert SSHConnection("axon")._forget_auto() is True
    connection._auto.clear()


def test_ssh_argv(monkeypatch):
    # a short home: the socket path falls back to a tmpdir hash when it is over ~90 chars
    # (pytest's macOS tmp_path is), which isn't what this test is about
    home = Path(tempfile.mkdtemp(prefix="rx", dir="/tmp"))
    monkeypatch.setenv("REMOTEEXEC_HOME", str(home))
    try:
        argv = SSHConnection("axon", port=2222)._ssh_argv("true")
        assert argv[0] == "ssh" and argv[-2:] == ["axon", "true"]
        assert "-p" in argv and f"ControlPath={home / 'cm' / 'axon-2222'}" in argv
    finally:
        shutil.rmtree(home, ignore_errors=True)


# ---- environments / templates ----
def test_wrap_command():
    assert wrap_command(CondaEnv("base"), "python x.py") == "python x.py"
    assert wrap_command(ApptainerEnv("/i.sif"), "python x.py") == \
        "apptainer exec --nv /i.sif bash -c 'python x.py'"
    assert wrap_command(UvEnv(python="3.11"), "python x.py") == \
        "uv run --python 3.11 -- bash -c 'python x.py'"


def test_render_command_job():
    s = render_sbatch(SlurmResources(job_name="go"), env=CondaEnv("base", init=True),
                      setup=[], cd_dir="/w/dir1", logdir="/w/.rx/s/logs",
                      command="python go.py --lr 3", pythonpath="/w/dir1:/w")
    assert "set +eu\npython go.py --lr 3\nrc=$?" in s
    assert 'export REMOTEEXEC_OUTPUT_DIR="/w/.rx/s/out"' in s
    assert 'export PYTHONPATH="/w/dir1:/w:' in s and "cd /w/dir1" in s
    assert "PYTHONUNBUFFERED=1" in s and "_runner.py" not in s


def test_render_array_outdir_is_per_task():
    s = render_sbatch(SlurmResources(array="0-2"), env=CondaEnv("base"), setup=[],
                      cd_dir="/w", logdir="/w/.rx/s/logs", runner_path="/w/.rx/s/_runner.py",
                      array_dir="/w/.rx/s")
    assert 'REMOTEEXEC_OUTPUT_DIR="/w/.rx/s/out/${SLURM_ARRAY_TASK_ID}"' in s


def test_safe_relpath():
    assert safe_relpath("./a/b.txt") == "a/b.txt"
    for bad in ("", "/etc/passwd", "../x", "a/../../x"):
        with pytest.raises(ValueError):
            safe_relpath(bad)


# ---- server ----
class FakeService:
    def __init__(self):
        self.cancelled = []

    def list_jobs(self, filters):
        return [{"job_id": "1", "filters": sorted(filters or [])}]

    def detail(self, jid):
        return {"job_id": jid} if jid == "1" else None

    def cancel(self, jid):
        self.cancelled.append(jid)
        return {"ok": True}

    def raw(self, jid, path):
        return 200, "image/png", b"\x89PNG"

    def preview(self, jid, path):
        return {"path": path}

    def script(self, jid):
        return {"text": "#!/bin/bash\n"}


@pytest.fixture
def server():
    from remoteexec.gui.server import create_server

    svc = FakeService()
    srv = create_server("127.0.0.1", 0, svc)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", svc
    srv.shutdown()


def _get(url, **headers):
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers)) as r:
        return r.status, r.headers.get("Content-Type"), r.read()


def test_server_routes(server):
    base, svc = server
    body = _get(base + "/api/jobs?filters=running,finished_6h")[2]
    assert json.loads(body)["jobs"][0]["filters"] == ["finished_6h", "running"]
    assert json.loads(_get(base + "/api/health")[2])["ok"] is True
    assert _get(base + "/api/jobs/1/file?path=a.png&raw=1")[1:] == ("image/png", b"\x89PNG")
    assert json.loads(_get(base + "/api/jobs/1/script")[2]) == {"text": "#!/bin/bash\n"}
    assert b"<title>remoteexec</title>" in _get(base + "/")[2]
    assert _get(base + "/static/app.js")[1].startswith("text/javascript")
    with pytest.raises(urllib.error.HTTPError) as ei:
        _get(base + "/api/jobs/2")
    assert ei.value.code == 404
    with pytest.raises(urllib.error.HTTPError):
        _get(base + "/static/..%2Fserver.py")


def test_server_guards(server):
    base, svc = server

    def post(**headers):
        return urllib.request.urlopen(urllib.request.Request(
            base + "/api/jobs/1/cancel", data=b"{}", method="POST", headers=headers))

    with pytest.raises(urllib.error.HTTPError) as ei:
        post()  # no X-Remoteexec header: a cross-site form can't add one
    assert ei.value.code == 403 and svc.cancelled == []
    with pytest.raises(urllib.error.HTTPError) as ei:
        post(**{"X-Remoteexec": "1", "Host": "evil.example:80"})  # DNS rebinding
    assert ei.value.code == 403
    assert json.loads(post(**{"X-Remoteexec": "1"}).read()) == {"ok": True}
    assert svc.cancelled == ["1"]


def test_unreachable_host_is_an_error_not_an_empty_list(rx_home):
    from remoteexec.accounting import batch_status
    from remoteexec.connection import CompletedRun
    from remoteexec.errors import ConnectionError
    from remoteexec.gui.service import JobService

    class Down:
        host = "axon"

        def run(self, cmd, **kw):
            return CompletedRun(255, "", "ssh: connect to host axon port 22: timed out")

    with pytest.raises(ConnectionError, match="timed out"):
        batch_status(Down(), ["1"])

    svc = JobService()
    svc._cluster = lambda host: type("C", (), {"conn": Down()})()
    assert svc._statuses("axon", ["1"]) == {"1": {}}
    assert "timed out" in svc.host_errors()["axon"]
