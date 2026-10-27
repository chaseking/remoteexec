"""Exception hierarchy — §21."""
from __future__ import annotations


class RemoteExecError(Exception):
    """Base class for all remoteexec errors."""


class EnvParityWarning(UserWarning):
    """The local Python/cloudpickle versions differ from the remote env in a way
    that can break by-value (un)pickling of functions and results (§26)."""


class ConnectionError(RemoteExecError):
    """SSH/ControlMaster connection failure."""


class TransferError(RemoteExecError):
    """rsync / file-transfer failure."""


class RemoteCommandError(RemoteExecError):
    """A remote command exited non-zero."""

    def __init__(self, cmd: str, returncode: int, stdout: str = "", stderr: str = ""):
        self.cmd = cmd
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        super().__init__(
            f"remote command failed (rc={returncode}): {cmd}\n{stderr or stdout}"
        )


class SubmissionError(RemoteExecError):
    """sbatch submission failed."""


class JobFailed(RemoteExecError):
    """A job reached a non-COMPLETED terminal state (OOM/TIMEOUT/cancel/node fail)."""

    def __init__(self, state, log_tail: str = ""):
        self.state = state
        self.log_tail = log_tail
        super().__init__(f"job ended in state {state}\n--- log tail ---\n{log_tail}")


class RemoteExecutionError(RemoteExecError):
    """The remote function itself raised."""

    def __init__(self, exc_type: str, remote_traceback: str, exception=None):
        self.exc_type = exc_type
        self.remote_traceback = remote_traceback
        self.exception = exception
        super().__init__(str(self))

    def __str__(self):
        return f"{self.exc_type} on cluster:\n{self.remote_traceback}"
