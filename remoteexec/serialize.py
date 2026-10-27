"""Payload/result (de)serialization with cloudpickle."""
from __future__ import annotations

import pickle
import platform
from pathlib import Path

from .errors import RemoteExecutionError

__version__ = "0.2.0"


def pack_payload(fn, args, kwargs) -> bytes:
    import cloudpickle

    inner = cloudpickle.dumps({"fn": fn, "args": args, "kwargs": kwargs})
    # The outer layer is plain pickle so the runner can read the version info
    # (and warn about a mismatch) even when the inner payload won't load.
    return pickle.dumps({
        "python_version": platform.python_version(),
        "cloudpickle_version": cloudpickle.__version__,
        "remoteexec_version": __version__,
        "payload": inner,
    })


def unpack_result(data: bytes):
    import cloudpickle

    out = cloudpickle.loads(data)
    if out["status"] == "ok":
        return out["result"]
    raise RemoteExecutionError(out["exc_type"], out["traceback"], out.get("exception"))


def load_result(path: Path):
    return unpack_result(Path(path).read_bytes())
