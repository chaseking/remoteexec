import os
import subprocess
from pathlib import Path
from typing import Optional

def _popen(cmd, title: Optional[str] = None, silent: bool = False, **kwargs):
    """Run a command and return the process return code."""
    if not silent:
        try:
            width = int(os.get_terminal_size().columns * 0.65)
        except OSError:
            width = 80
        # print("")
        print("╔" + "═"*width + "╗")
        if title is not None:
            print(f"║  {title}")
            print("╟" + "─"*width + "╢")
    output_lines = []
    with subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        encoding="utf-8",
        bufsize=1,  # Line-buffered output
        **kwargs
    ) as process:
        for line in process.stdout:
            output_lines.append(line)
            if not silent:
                print("║ ", line, end="")
        return_code = process.wait()
    if not silent:
        print("╚" + "═"*width + f" --> {return_code}")
        # print("")
    return return_code, output_lines


def rsync(src: any, dst: str, args: Optional[list[str]] = None, verbose: bool = True, title: Optional[str] = "Syncing {src} to {dst}"):
    """Sync a file to a remote host using rsync."""
    src = str(src)
    if title is not None:
        title = title.format(src=src, dst=dst)
    command = ["rsync", "-avz" if verbose else "-az", "--progress"]
    if args is not None:
        command.extend(args)
    command.append(src)
    command.append(dst)
    return _popen(command, title=title)

def ssh_exec(remote: str, command: str | list[str], title: Optional[str] = None, **kwargs):
    """Execute a command on a remote host using ssh."""
    full_command = ["ssh", remote]
    if isinstance(command, str):
        full_command.append(command)
    else:
        full_command.extend(command)
    return _popen(full_command, title=title, **kwargs)

def ssh_exec_cd_and_python(
    remote: str,
    dirname: str,
    filename: str,
    python: str = "python",
    args: Optional[str | list[str]] = None,
    title: Optional[str] = "[@{remote}]> {command}",
):
    """Execute a command on a remote host using ssh."""
    dirname = str(dirname)
    filename = str(filename)
    command = f"cd {dirname} && {python} {filename}"
    if args is not None:
        if isinstance(args, str):
            command += " " + args
        else:
            command += " " + " ".join(args)
    if title is not None:
        title = title.format(remote=remote, dirname=dirname, filename=filename, command=command)
    return ssh_exec(remote=remote, command=command, title=title)
    