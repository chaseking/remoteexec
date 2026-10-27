"""Serialization — §11."""
from __future__ import annotations

from pathlib import Path

from .errors import RemoteExecutionError

__version__ = "0.2.0"


def pack_payload(fn, args, kwargs) -> bytes:
    import pickle
    # import sys
    import platform  # gives a simple version rather than the build details in sys.version
    import cloudpickle

    inner = cloudpickle.dumps({"fn": fn, "args": args, "kwargs": kwargs})
    outer = {
        "python_version": platform.python_version(),
        "cloudpickle_version": cloudpickle.__version__,
        "remoteexec_version": __version__,
        "payload": inner,
    }
    return pickle.dumps(outer)


def load_result(path: Path):
    import cloudpickle

    with open(path, "rb") as fh:
        out = cloudpickle.load(fh)
    if out["status"] == "ok":
        return out["result"]
    raise RemoteExecutionError(out["exc_type"], out["traceback"], out.get("exception"))
