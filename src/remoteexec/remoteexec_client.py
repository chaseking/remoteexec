import argparse
from pathlib import Path
from os.path import join as path_join
import sys
from shlex import quote as _quote_cmdline_str

from .base import rsync, ssh_exec, ssh_exec_cd_and_python, _popen

def main():
    parser = argparse.ArgumentParser(description="Execute file remotely.")
    parser.add_argument("executable", nargs=argparse.REMAINDER, help="Remote executable (e.g., 'python script.py')")
    parser.add_argument("--remote", type=str, required=True, help="SSH of the remote server")
    parser.add_argument("--parent", type=str, default=None, help="Parent directory to copy to remote. Defaults to cwd.")
    # parser.add_argument("--dirname_prefix", type=str, default="remoteexec_", help="Remote directory name prefix.")
    parser.add_argument("--dst", type=str, default="~/_remoteexec_srcs/", help="Destination directory on remote server.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output.")

    args = parser.parse_args()
    args.executable = " ".join(args.executable)
    if not args.executable:
        print("No executable provided; supply a command.")
        sys.exit(1)
    args.parent = Path(args.parent).resolve() if args.parent else Path.cwd()

    if not args.parent.exists():
        print(f"Parent directory {args.parent} does not exist.")
        sys.exit(1)
    
    if not args.verbose:
        print(f"Copying {args.parent} to {args.remote}:{args.dst} ...", end="", flush=True)
    rsync(
        src=args.parent,
        dst=f"{args.remote}:{args.dst}",
        args=["--exclude", ".git/"],  # TODO: customizable?
        silent=(not args.verbose),
    )
    if not args.verbose:
        print(" done")
    dir_on_remote = path_join(args.dst, args.parent.name)

    # Run the job
    # command = f"cd {dir_on_remote} && 'bash -l -c \"{args.executable}\"'"
    command = ["cd", dir_on_remote, "&&", "bash", "-l", "-c", _quote_cmdline_str(args.executable)]
    ssh_exec(
        remote = args.remote,
        command = command,
        title = f"[{args.remote}:{dir_on_remote}] > {args.executable}",
    )
    
    # return_code, output_lines = ssh_exec_cd_and_python(
    #     remote=args.remote,
    #     dirname=path_join(args.dst, args.parent.name),
    #     filename=relative_file,
    #     args=[*sys.argv[1:]],
    # )
    # if return_code != 0:
    #     print(f"Error in job (return code {return_code})")
    #     return


    # job_details = output_lines[-1]  # see (*), last line in main_remote
    # del output_lines
    # from ast import literal_eval
    # try:
    #     job_details = literal_eval(job_details)
    # except Exception as e:
    #     # print("Failed to parse job details:", e)
    #     return
    
    # if not job_details["success"]:
    #     print("Job failed:", job_details)
    #     return
    
    # if job_details["is_array_task"]:
    #     # print("Job is an array task, no viewing option available.")
    #     return
    
    # log_file = job_details["log_file"]
    # print()
    # print(f"Below is the log file of the job (ID {job_details['job_id']}) (path: {REMOTE_NAME}:{log_file})")
    # print("Press Ctrl+C to exit log viewer and leave task running in background.")
    # print("Press Ctrl+C twice to exit this log viewer and CANCEL task.")
    # print()
    # wait_seconds = 3
    # try:
    #     ssh_exec(remote=REMOTE_NAME, command=f"tail --retry -f {log_file}", title=None)
    # except KeyboardInterrupt:
    #     print(f"\nExiting log viewer. Press Ctrl+C again to cancel task, otherwise wait {wait_seconds} seconds.")
    
    # import time
    # try:
    #     time.sleep(wait_seconds)
    #     print(f"Exiting log viewer. Task {job_details['job_id']} is still running in background.")
    # except KeyboardInterrupt:
    #     print(f"\nCancelling task with ID {job_details['job_id']}...")
    #     return_code, _ = ssh_exec(remote=REMOTE_NAME, command=f"scancel {job_details['job_id']}", silent=True)
    #     if return_code == 0:
    #         print("Task cancelled.")
    #     else:
    #         print(f"Failed to cancel task (return code {return_code})")

    # print(args)
