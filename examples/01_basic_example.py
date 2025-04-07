from pathlib import Path

from remoteexec import *

to_copy = Path(__file__).parent / "hello_world.py"
remote = "axon"
remote_dir = "~/TEST/"

rsync(src=to_copy, dst=f"{remote}:{remote_dir}")
ssh_exec_cd_and_python(remote=remote, dirname=remote_dir, filename=to_copy.name)
