"""Remote-side entrypoint — §12. Stdlib + cloudpickle only.

Copied into each session dir and invoked by absolute path, so the cluster needs
no ``remoteexec`` install. Reused for local subprocess runs too.
"""
import os
import pickle
import sys
import traceback


def _version_warning(payload):
    import platform
    import cloudpickle

    host_py = payload.get("python_version")
    host_cp = payload.get("cloudpickle_version")
    remote_py = platform.python_version()
    remote_cp = cloudpickle.__version__

    mismatches = []
    if host_py and host_py.split(".")[:2] != remote_py.split(".")[:2]:
        mismatches.append(f"  python:      host={host_py}  remote={remote_py}")
    if host_cp and host_cp != remote_cp:
        mismatches.append(f"  cloudpickle: host={host_cp}  remote={remote_cp}")

    if mismatches:
        print(
            "\n"
            "WARNING: environment mismatch between host and remote — you are likely to encounter an error.\n"
            + "\n".join(mismatches)
            + "\n",
            file=sys.stderr,
        )


def main():
    import cloudpickle

    payload_path, result_path = sys.argv[1], sys.argv[2]
    with open(payload_path, "rb") as fh:
        outer = pickle.load(fh)
    _version_warning(outer)
    inner = cloudpickle.loads(outer["payload"])
    fn, args, kwargs = inner["fn"], inner["args"], inner["kwargs"]
    try:
        out = {"status": "ok", "result": fn(*args, **kwargs)}
    except BaseException as e:  # capture everything
        traceback.print_exc()  # always print to the log, even if the exception isn't serializable
        out = {
            "status": "error",
            "exc_type": type(e).__name__,
            "traceback": traceback.format_exc(),
            "exception": _safe(e),
        }
    tmp = result_path + ".tmp"
    with open(tmp, "wb") as fh:
        cloudpickle.dump(out, fh)
    os.replace(tmp, result_path)  # atomic publish
    sys.exit(0 if out["status"] == "ok" else 1)


def _safe(exc):
    try:
        pickle.loads(pickle.dumps(exc))
        return exc
    except Exception:
        return None  # traceback string is always kept


if __name__ == "__main__":
    main()
