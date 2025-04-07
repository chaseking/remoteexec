from pathlib import Path
import subprocess
from typing import Optional

from remoteexec.base import _popen

to_copy = Path().resolve().parent / "examples" / "hello_world.py"
remote_host = "axon"
remote_dir = "~/TEST/"

_popen([
    "rsync", "-avz", "--progress",
    str(to_copy), f"{remote_host}:{remote_dir}"
], title=f"Syncing {to_copy} to {remote_host}:{remote_dir}")

_popen([
    "ssh", f"{remote_host}",
    f"cd {remote_dir} && python {to_copy.name}"
], title="Running python script on remote host")