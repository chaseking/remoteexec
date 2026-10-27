"""Helpers called *inside* a job. Stdlib only: this runs on the cluster."""
from __future__ import annotations

import os
from pathlib import Path


def is_remote() -> bool:
    """True inside a remotely-executing job; False for local runs."""
    return os.environ.get("REMOTEEXEC_REMOTE") == "1"


def output_dir() -> Path:
    """This job's private output directory (created on first use).

    On the cluster it is ``<run dir>/out`` (``out/<task>`` for array tasks),
    so concurrent jobs never overwrite each other's files, and
    ``job.outputs()`` / ``job.fetch_outputs()`` / the GUI find them without
    being told where to look. Local runs (``fn.local()``) use
    ``./remoteexec-out/<name>-local``. Shell code can use
    ``$REMOTEEXEC_OUTPUT_DIR``.
    """
    p = Path(os.environ.get("REMOTEEXEC_OUTPUT_DIR") or "remoteexec-out/local")
    p.mkdir(parents=True, exist_ok=True)
    return p
