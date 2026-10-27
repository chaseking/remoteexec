"""Offline unit tests for the GUI service helpers — no cluster needed."""
from __future__ import annotations

from datetime import datetime, timedelta

from remoteexec.clusters.slurm import expand_array_spec
from remoteexec.environment import CondaEnv, RawSetup, UvEnv, env_label
from remoteexec.gui.service import (
    DASH,
    build_row,
    format_elapsed,
    format_started,
    is_running,
    matches_filters,
    parse_slurm_time,
    started_iso,
    window_start,
)


def test_started_iso():
    # prefers sacct start, falls back to submit time, "" when unknown
    assert started_iso("2026-06-18T08:37:52", "x") == "2026-06-18T08:37:52"
    assert started_iso("Unknown", "2026-06-18T07:00:00") == "2026-06-18T07:00:00"
    assert started_iso("", "") == ""


def test_env_label():
    assert env_label(CondaEnv("base")) == "conda:base"
    assert env_label(UvEnv()) == "uv"
    assert env_label(RawSetup("")) == "raw"
    assert env_label(None) == ""


def test_format_elapsed():
    assert format_elapsed("00:00:42") == "00:42"
    assert format_elapsed("01:02:03") == "01:02:03"
    assert format_elapsed("1-02:03:04") == "1d 02:03:04"
    assert format_elapsed("12:34") == "12:34"
    assert format_elapsed("") == DASH
    assert format_elapsed("Unknown") == DASH


def test_parse_and_started():
    assert parse_slurm_time("2026-06-18T08:37:52") == datetime(2026, 6, 18, 8, 37, 52)
    assert parse_slurm_time("Unknown") is None
    # prefers sacct start, falls back to submitted_at
    assert format_started("2026-06-18T08:37:52", "x") == "2026-06-18 08:37"
    assert format_started("Unknown", "2026-06-18T07:00:00") == "2026-06-18 07:00"
    assert format_started("", "") == DASH


def test_is_running():
    assert is_running("RUNNING")
    assert is_running("PENDING")
    assert not is_running("COMPLETED")
    assert not is_running("FAILED")


def test_matches_filters():
    now = datetime(2026, 6, 18, 12, 0, 0)
    # running job matches only the running filter
    assert matches_filters("RUNNING", None, {"running"}, now)
    assert not matches_filters("RUNNING", None, {"finished_1d"}, now)
    # finished 2h ago -> in 6h/1d/7d windows
    end = now - timedelta(hours=2)
    assert matches_filters("COMPLETED", end, {"finished_6h"}, now)
    assert matches_filters("COMPLETED", end, {"finished_1d"}, now)
    assert not matches_filters("COMPLETED", end, {"running"}, now)
    # finished 3 days ago -> only in 7d window
    end = now - timedelta(days=3)
    assert not matches_filters("COMPLETED", end, {"finished_6h"}, now)
    assert not matches_filters("COMPLETED", end, {"finished_1d"}, now)
    assert matches_filters("COMPLETED", end, {"finished_7d"}, now)
    # union semantics
    assert matches_filters("RUNNING", None, {"running", "finished_7d"}, now)
    # empty filters -> everything shows
    assert matches_filters("COMPLETED", end, set(), now)


def test_window_start_covers_the_widest_filter():
    now = datetime(2026, 6, 18, 12, 0, 0)
    # the window must reach past the oldest thing a filter can select, or the
    # table would drop jobs the user asked to see
    assert window_start({"finished_6h"}, now) < now - timedelta(hours=6)
    assert window_start({"finished_6h", "finished_7d"}, now) < now - timedelta(days=7)
    # "running" alone still needs a margin, not a zero-width window
    assert window_start({"running"}, now) < now
    # no filters means "show everything", so reach much further back
    assert window_start(set(), now) < now - timedelta(days=29)


def test_expand_array_spec():
    # squeue reports pending array tasks compactly; a literal match would miss
    # every one of them
    assert expand_array_spec("123_[2-5]") == ["123_2", "123_3", "123_4", "123_5"]
    assert expand_array_spec("123_[0-2%2]") == ["123_0", "123_1", "123_2"]
    assert expand_array_spec("123_[1,4-5]") == ["123_1", "123_4", "123_5"]
    # already-expanded and ordinary ids pass through untouched
    assert expand_array_spec("123_7") == ["123_7"]
    assert expand_array_spec("123") == ["123"]


def test_build_row_uses_the_allocation_number_for_arrays():
    rec = {"cluster_host": "axon", "session": "s", "workdir": "/w",
           "logdir": "/w/.rx/s/logs", "name": "sweep[1]",
           "submitted_at": "2026-08-17T14:02:00"}
    live = {"state": "RUNNING", "start": "", "end": "", "elapsed": "00:01:00",
            "exitcode": "", "raw": "6214391"}
    row = build_row("6214389_1", rec, live)
    assert row["logfile"] == "/w/.rx/s/logs/6214391.out"
    # without an allocation number, fall back to the id
    assert build_row("6214389_1", rec, {})["logfile"] == "/w/.rx/s/logs/6214389_1.out"


def test_build_row_backward_compat():
    # an old manifest entry missing env/local_dir/partition still renders
    rec = {
        "cluster_host": "axon",
        "session": "s1",
        "workdir": "/home/u/remoteexec/proj/s1",
        "logdir": "/home/u/remoteexec/proj/s1/logs",
        "name": "train",
        "submitted_at": "2026-06-18T07:00:00",
    }
    row = build_row("123", rec, None)
    assert row["job_id"] == "123"
    assert row["name"] == "train"
    assert row["status"] == "UNKNOWN"
    assert row["env"] == DASH
    assert row["partition"] == DASH
    # falls back to workdir when local_dir absent
    assert row["project_path"] == "/home/u/remoteexec/proj/s1"
    assert row["logfile"].endswith("/123.out")

    # with live info + new fields
    rec2 = {**rec, "env": "conda:base", "local_dir": "/Users/me/proj",
            "partition": "ctn"}
    live = {"state": "RUNNING", "start": "2026-06-18T07:01:00",
            "elapsed": "00:05:00", "end": "", "exitcode": "0:0"}
    row2 = build_row("123", rec2, live)
    assert row2["status"] == "RUNNING"
    assert row2["env"] == "conda:base"
    assert row2["partition"] == "ctn"
    assert row2["project_path"] == "/Users/me/proj"
    assert row2["running"] == "05:00"
    assert row2["started"] == "2026-06-18 07:01"
