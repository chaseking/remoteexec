"""Offline tests for incremental log reading — no cluster needed.

The fake connection stands in for SSH: it runs the generated shell script's
*intent* against an in-memory file, so the offset arithmetic is exercised
without a remote host.
"""
from __future__ import annotations

import re

import pytest

from remoteexec.connection import CompletedRun
from remoteexec.logtail import (
    log_filename,
    read_chunk,
    resolve_raw_id,
    trim_incomplete_utf8,
)


class FakeConn:
    """Answers the logtail script for an in-memory 'file'."""

    def __init__(self, content: bytes | None):
        self.content = content
        self.calls = 0

    def run(self, cmd, *, check=True, timeout=None, text=True):
        assert text is False, "log reads must be raw bytes"
        self.calls += 1
        offset = int(re.search(r"^start=(\d+)$", cmd, re.M).group(1))
        tail = int(re.search(r"^tail=(\d+)$", cmd, re.M).group(1))
        max_bytes = int(re.search(r"head -c (\d+)", cmd).group(1))
        if self.content is None:
            return CompletedRun(0, b"-1\n", b"")
        size = len(self.content)
        if size < offset:
            return CompletedRun(0, f"{size}\n".encode(), b"")
        start = size - tail if offset == 0 and tail and size > tail else offset
        data = self.content[start:start + max_bytes]
        return CompletedRun(0, f"{size}\n{start}\n".encode() + data, b"")


def test_offset_counts_file_bytes_not_decoded_text():
    # A log with carriage returns (progress bars) and a non-UTF-8 byte: the old
    # len(text.encode()) accounting under-counted both, so the next read started
    # too early and replayed output the viewer had already shown.
    content = b"step 1\r\nstep 2\rstep 3\n\xff done\n"
    conn = FakeConn(content)
    first = read_chunk(conn, "/log", 0)
    assert first.next_offset == len(content)
    assert "step 1" in first.text and "step 3" in first.text
    # nothing new appended -> second read returns nothing at all (no repeat)
    second = read_chunk(conn, "/log", first.next_offset)
    assert second.text == ""
    assert second.next_offset == len(content)


def test_incremental_reads_do_not_overlap_or_gap():
    content = b"aaa\r\nbbb\r\n"
    conn = FakeConn(content)
    c1 = read_chunk(conn, "/log", 0)
    conn.content += b"ccc\r\n"
    c2 = read_chunk(conn, "/log", c1.next_offset)
    assert c1.text + c2.text == "aaa\r\nbbb\r\nccc\r\n"


def test_chunk_cap_and_more_flag():
    conn = FakeConn(b"x" * 100)
    c1 = read_chunk(conn, "/log", 0, max_bytes=40)
    assert len(c1.text) == 40 and c1.more is True
    c2 = read_chunk(conn, "/log", c1.next_offset, max_bytes=40)
    c3 = read_chunk(conn, "/log", c2.next_offset, max_bytes=40)
    assert c1.text + c2.text + c3.text == "x" * 100
    assert c3.more is False


def test_initial_tail_skips_history():
    conn = FakeConn(b"old" * 100 + b"NEW")
    c = read_chunk(conn, "/log", 0, initial_tail=3)
    assert c.text == "NEW"
    assert c.skipped == 300
    assert c.next_offset == 303
    # only the first read skips; later ones continue from the offset
    conn.content += b"MORE"
    c2 = read_chunk(conn, "/log", c.next_offset, initial_tail=3)
    assert c2.text == "MORE" and c2.skipped == 0


def test_missing_file_keeps_offset():
    c = read_chunk(FakeConn(None), "/log", 42)
    assert c.exists is False and c.stalled is False and c.next_offset == 42


def test_short_file_holds_position_instead_of_replaying():
    # A file reading as shorter than our offset (a stale size over NFS) must
    # never rewind: re-reading from 0 would duplicate the whole log on screen.
    c = read_chunk(FakeConn(b"short"), "/log", 500)
    assert c.exists is True and c.stalled is True
    assert c.next_offset == 500 and c.text == ""


def test_log_filename_uses_the_allocation_number():
    # ordinary job: the id names the file
    assert log_filename("/logs", "123") == "/logs/123.out"
    # array task: Slurm's %j is the task's own allocation number, which is not
    # derivable from the <array>_<index> id (6214389_1 -> 6214391)
    assert log_filename("/logs", "6214389_1", "6214391") == "/logs/6214391.out"
    # unknown allocation number: fall back to the id rather than guessing
    assert log_filename("/logs", "6214389_1", "") == "/logs/6214389_1.out"


class SacctConn:
    def __init__(self, out):
        self.out = out
        self.calls = 0

    def run(self, cmd, *, check=True, timeout=None, text=True):
        self.calls += 1
        return CompletedRun(0, self.out, "")


def test_resolve_raw_id():
    out = ("6214389_1|6214391\n"
           "6214389_1.extern|6214391.extern\n"
           "6214389_2|6214392\n")
    conn = SacctConn(out)
    assert resolve_raw_id(conn, "6214389_1") == "6214391"
    # plain job ids never hit sacct at all
    assert resolve_raw_id(conn, "555") == "555"
    assert conn.calls == 1
    # no accounting row -> fall back to the id we were given
    assert resolve_raw_id(SacctConn(""), "6214389_9") == "6214389_9"


def test_split_multibyte_char_is_held_back():
    content = "héllo wörld".encode()  # é and ö are two bytes each
    conn = FakeConn(content)
    # a cap that lands in the middle of the "é"
    c1 = read_chunk(conn, "/log", 0, max_bytes=2)
    assert "�" not in c1.text
    assert c1.next_offset == 1  # the lead byte of é was held back
    rest = read_chunk(conn, "/log", c1.next_offset, max_bytes=100)
    assert c1.text + rest.text == "héllo wörld"


@pytest.mark.parametrize("raw,kept", [
    (b"", b""),
    (b"abc", b"abc"),
    (b"abc\xc3", b"abc"),            # lead byte of a 2-byte seq, incomplete
    (b"abc\xc3\xa9", b"abc\xc3\xa9"),  # complete
    (b"abc\xe2\x82", b"abc"),        # 3-byte seq, one byte short
    (b"abc\xe2\x82\xac", b"abc\xe2\x82\xac"),
    (b"abc\xf0\x9f\x98", b"abc"),    # 4-byte seq, one byte short
])
def test_trim_incomplete_utf8(raw, kept):
    assert trim_incomplete_utf8(raw) == kept
