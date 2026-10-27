"""CLI against the fake cluster."""
from __future__ import annotations

import pytest

from conftest import make_cluster
from remoteexec import cli, config, hosts


@pytest.fixture
def rx(fake_slurm, project, tmp_path, monkeypatch, capsys):
    cluster = make_cluster(tmp_path)
    monkeypatch.setattr(config, "resolve_cluster", lambda name=None, **kw: cluster)
    monkeypatch.setattr(cli, "_cluster_for_host", lambda host: cluster)

    def run(*argv):
        with pytest.raises(SystemExit) as ei:
            cli.main(list(argv))
        out = capsys.readouterr()
        return ei.value.code, out.out, out.err

    return run


def test_run_wait_outputs_cat_fetch(rx, project):
    (project / "go.py").write_text(
        "import os\nopen(os.environ['REMOTEEXEC_OUTPUT_DIR'] + '/a.txt', 'w').write('hi')\nprint('done!')\n")
    rc, out, _ = rx("run", "-p", "ctn", "--", "python", "go.py")
    jid = out.strip()
    assert rc == 0 and jid.isdigit()

    rc, out, _ = rx("wait", "last", "--timeout", "60")
    assert rc == 0
    assert f"{jid} go: COMPLETED" in out and "done!" in out and "outputs (1): a.txt 2B" in out

    assert rx("outputs")[1] == "    2B  a.txt\n"
    assert rx("cat", jid, "a.txt")[1] == "hi"
    rx("fetch", jid)
    assert (project / "remoteexec-out" / f"go-{jid}" / "a.txt").read_text() == "hi"
    assert "done!" in rx("logs", jid, "-n", "5")[1]

    rc, out, _ = rx("ls")
    assert jid in out and "COMPLETED" in out and "go" in out


def test_wait_exit_codes(rx):
    rc, out, _ = rx("run", "-n", "bad", "--wait", "--timeout", "60", "exit 4")
    assert rc == 1 and "FAILED" in out
    rc, out, _ = rx("run", "-n", "slow", "sleep 20")
    rc, out, _ = rx("wait", out.strip(), "--timeout", "1", "--tail", "0")
    assert rc == 2 and "still running" in out
    rx("cancel", "last")


def test_unknown_job(rx):
    rc, _, err = rx("logs", "999999")
    assert rc == 1 and "unknown job 999999" in err


def test_host_routing_commands(rx):
    rx("host", "add", "axon", "axon-remote")
    assert hosts.lookup("axon-remote") == ("axon", ["axon", "axon-remote"], "axon")
    rx("host", "use", "axon", "axon-remote")
    rc, out, _ = rx("host")
    assert "axon: axon axon-remote  (active: axon-remote)" in out
    rx("host", "rm", "axon")
    assert hosts.load() == {}


def test_skill_print_and_install(rx, project):
    rc, out, _ = rx("skill")
    assert out.startswith("---\nname: remoteexec")
    rx("skill", "--install", "--project")
    assert (project / ".claude" / "skills" / "remoteexec" / "SKILL.md").read_text() == out


def test_clean_is_a_dry_run_unless_confirmed(rx):
    rx("run", "true")
    rc, out, _ = rx("clean", "--older-than", "0")
    assert "would remove 1" in out and "dry run" in out
    assert rx("ls")[1].count("\n") == 2  # header + the job
    rc, out, _ = rx("clean", "--older-than", "0", "-y")
    assert "removed 1" in out
    assert "no jobs" in rx("ls")[1]
