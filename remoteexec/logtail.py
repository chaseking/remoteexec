"""Byte-exact incremental log reading, shared by ``Job.stream`` and the GUI.

Logs are read by re-opening the file at a tracked *byte* offset on every poll
rather than holding a ``tail -f`` open: on NFS a long-lived fd does not see the
bytes a compute node appends, while a fresh open() forces a revalidation.

That scheme only works if the offset is counted in the file's own bytes. The
obvious ``offset += len(chunk.encode())`` is wrong, because the chunk has
already been through ``subprocess``' text mode by then:

* universal newlines rewrites ``\\r\\n`` and lone ``\\r`` to ``\\n`` — one byte
  lost per occurrence, and progress bars (tqdm & co) emit a ``\\r`` per update;
* undecodable bytes become U+FFFD, which re-encodes to a different length.

Every lost byte makes the next read start too early, so the viewer is sent a
slice of the log it has already displayed — the duplicated lines. Here the
chunk is fetched as raw bytes and the offset is advanced by exactly what the
remote sent, decoding only for display.
"""
from __future__ import annotations

from dataclasses import dataclass

from .paths import rq

# Cap on one read, so a job that dumped gigabytes cannot turn a single poll into
# a multi-minute transfer that blocks the request (and the reader) behind it.
MAX_CHUNK = 1 << 20  # 1 MiB


@dataclass
class LogChunk:
    """One incremental read of a remote log file."""

    text: str = ""
    next_offset: int = 0
    exists: bool = False
    stalled: bool = False  # file is shorter than our offset (see read_chunk)
    skipped: int = 0  # bytes deliberately jumped over before this chunk
    more: bool = False  # bytes are already waiting; poll again without delay
    size: int = 0


def log_filename(logdir: str, job_id: str, raw_id: str = "") -> str:
    """Path of the combined log for a job.

    The sbatch template writes ``--output={logdir}/%j.out``, and Slurm expands
    ``%j`` to the job's *own* allocation number. For an array task that is not
    the ``<array>_<index>`` id the manifest is keyed by — task ``6214389_1``
    can be allocation ``6214391`` — and the mapping is assigned at scheduling
    time, so it has to be looked up (see :func:`resolve_raw_id`) rather than
    computed. ``raw_id`` is that number; for ordinary jobs it equals ``job_id``.
    """
    return f"{logdir}/{raw_id or job_id}.out"


def resolve_raw_id(conn, job_id: str, *, timeout=None) -> str:
    """Look up the Slurm allocation number behind an array task id.

    Returns *job_id* unchanged for ordinary jobs, and as a fallback when
    accounting has no record of the task.
    """
    jid = str(job_id)
    if "_" not in jid:
        return jid
    out = conn.run(
        f"sacct -j {jid} --format=JobID,JobIDRaw -Pn 2>/dev/null",
        check=False, timeout=timeout,
    ).stdout
    for line in out.splitlines():
        parts = line.split("|")
        if len(parts) >= 2 and parts[0].strip() == jid:
            return parts[1].strip() or jid
    return jid


def trim_incomplete_utf8(raw: bytes) -> bytes:
    """Drop a trailing partial UTF-8 sequence from *raw*.

    A chunk boundary can fall inside a multi-byte character. Decoding it with
    ``errors="replace"`` would burn the split character into the viewer as a
    U+FFFD *and* leave its tail bytes to be decoded as garbage next time, so
    hold the incomplete sequence back and let the next read pick it up whole.
    """
    for back in range(1, min(4, len(raw)) + 1):
        b = raw[-back]
        if b < 0x80:  # ASCII — nothing after it can be a partial sequence
            break
        if b >= 0xC0:  # lead byte: how long should the sequence be?
            need = 2 if b < 0xE0 else 3 if b < 0xF0 else 4
            return raw[:-back] if back < need else raw
        # 0x80..0xBF is a continuation byte; keep walking backwards
    return raw


def _script(logfile: str, offset: int, max_bytes: int, initial_tail: int) -> str:
    """Shell that reports the log's size and where it read from, then the bytes.

    One round trip has to answer three questions — does the file exist, was it
    truncated since last time, and where did the read actually start — so the
    header lines carry that and the payload follows:

        -1\\n                     file does not exist
        <size>\\n                 file is shorter than the requested offset
        <size>\\n<start>\\n<raw>   normal read
    """
    f = rq(logfile)
    return (
        f'f={f}\n'
        'if [ ! -e "$f" ]; then echo -1; exit 0; fi\n'
        'sz=$(wc -c < "$f" 2>/dev/null | tr -d " ")\n'
        '[ -n "$sz" ] || sz=0\n'
        'echo "$sz"\n'
        f'start={int(offset)}\n'
        # size < offset means the file was truncated or replaced under us
        'if [ "$sz" -lt "$start" ]; then exit 0; fi\n'
        f'tail={int(initial_tail)}\n'
        'if [ "$start" -eq 0 ] && [ "$tail" -gt 0 ] && [ "$sz" -gt "$tail" ]; then\n'
        '  start=$((sz - tail))\n'
        'fi\n'
        'echo "$start"\n'
        f'tail -c +$((start + 1)) "$f" 2>/dev/null | head -c {int(max_bytes)}\n'
    )


def read_chunk(conn, logfile: str, offset: int = 0, *, timeout=None,
               max_bytes: int = MAX_CHUNK, initial_tail: int = 0) -> LogChunk:
    """Read up to *max_bytes* of ``logfile`` starting at byte *offset*.

    ``initial_tail``, when set, caps how much history a first read (``offset``
    0) pulls in: it starts that many bytes from the end and reports the rest as
    ``skipped``. Opening a viewer on a job with a huge log then costs one small
    read instead of the whole file.
    """
    out = conn.run(
        _script(logfile, offset, max_bytes, initial_tail),
        check=False, timeout=timeout, text=False,
    ).stdout
    size_line, _, rest = out.partition(b"\n")
    try:
        size = int(size_line.strip() or -1)
    except ValueError:  # shell noise where a number was due — treat as missing
        return LogChunk(next_offset=offset)
    if size < 0:
        return LogChunk(next_offset=offset)  # not created yet; keep our place
    start_line, sep, data = rest.partition(b"\n")
    if not sep:
        # The size header arrived alone: the file reads as shorter than the
        # offset we already consumed. Hold position rather than re-reading from
        # the top — over NFS a stale cached size is far more likely than a real
        # truncation (job logs live in a fresh per-run directory and are never
        # rewritten), and replaying the file would duplicate everything on
        # screen. A blip corrects itself on the next poll.
        return LogChunk(next_offset=offset, exists=True, stalled=True, size=size)
    try:
        start = int(start_line.strip())
    except ValueError:
        return LogChunk(next_offset=offset, exists=True, size=size)

    data = trim_incomplete_utf8(data)
    return LogChunk(
        text=data.decode("utf-8", "replace"),
        next_offset=start + len(data),
        exists=True,
        skipped=max(0, start - offset),
        more=size > start + len(data),
        size=size,
    )
