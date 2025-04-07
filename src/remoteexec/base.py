import os
import subprocess
from pathlib import Path
from typing import Optional


def _popen(cmd, title: Optional[str] = None, silent: bool = False, ignore_line = None, end_check = None, **kwargs):
    """Run a command and return the process return code."""
    if not silent:
        try:
            width = int(os.get_terminal_size().columns * 0.65)
        except OSError:
            width = 80
        # print("")
        print("╔" + "═"*width + "╗")
        if title is not None:
            print(f"║ {title}")
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
            if not silent and (ignore_line is None or not ignore_line(line)):
                print("║", line, end="")
            if end_check is not None and end_check(line):
                process.terminate()
        return_code = process.wait()
    if not silent:
        print("╚" + "═"*width + f" --> {return_code}")
        # print("")
    return return_code, output_lines


def rsync(src: any, dst: str, args: Optional[list[str]] = None, title: Optional[str] = "Syncing {src} to {dst}", **kwargs):
    """Sync a file to a remote host using rsync."""
    src = str(src)
    if title is not None:
        title = title.format(src=src, dst=dst)
    command = ["rsync", "-avz", "--progress"]
    if args is not None:
        command.extend(args)
    command.append(src)
    command.append(dst)
    return _popen(command, title=title, **kwargs)

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
    