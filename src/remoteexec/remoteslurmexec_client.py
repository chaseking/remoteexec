from slurmexec import *

if is_this_a_slurm_job():
    try:
        import jax
        jax.config.update("jax_enable_x64", True)
        jax.config.update("jax_platform_name", jax.default_backend())  # GPU if available
    except ImportError:
        pass

REMOTE_NAME = "axon"

def main_local(this_file, conda_env, **kwargs):
    import sys
    from pathlib import Path
    from remoteexec import rsync, ssh_exec, ssh_exec_cd_and_python
    from remoteexec.base import _popen
    
    ROOT_DIR = Path(__file__).parent

    rsync(
        src=ROOT_DIR,
        dst=f"{REMOTE_NAME}:~",
        args=["--exclude", ".git/"],
    )
    
    # Run the job
    this_file_relative = Path(this_file).relative_to(ROOT_DIR)
    return_code, output_lines = ssh_exec_cd_and_python(
        remote=REMOTE_NAME,
        dirname=f"~/{ROOT_DIR.name}",
        filename=this_file_relative,
        args=[*sys.argv[1:]],
    )
    if return_code != 0:
        print(f"Error in job (return code {return_code})")
        return

    job_details = output_lines[-1]  # see (*), last line in main_remote
    del output_lines
    from ast import literal_eval
    try:
        job_details = literal_eval(job_details)
    except Exception as e:
        # print("Failed to parse job details:", e)
        return
    
    if not job_details["success"]:
        print("Job failed:", job_details)
        return
    
    if job_details["is_array_task"]:
        # print("Job is an array task, no viewing option available.")
        return
    
    log_file = job_details["log_file"]
    print()
    print(f"Below is the log file of the job (ID {job_details['job_id']}) (path: {REMOTE_NAME}:{log_file})")
    print("Press Ctrl+C to exit log viewer and leave task running in background.")
    print("Press Ctrl+C twice to exit this log viewer and CANCEL task.")
    print()
    wait_seconds = 3
    try:
        ssh_exec(remote=REMOTE_NAME, command=f"tail --retry -f {log_file}", title=None)
    except KeyboardInterrupt:
        print(f"\nExiting log viewer. Press Ctrl+C again to cancel task, otherwise wait {wait_seconds} seconds.")
    
    import time
    try:
        time.sleep(wait_seconds)
        print(f"Exiting log viewer. Task {job_details['job_id']} is still running in background.")
    except KeyboardInterrupt:
        print(f"\nCancelling task with ID {job_details['job_id']}...")
        return_code, _ = ssh_exec(remote=REMOTE_NAME, command=f"scancel {job_details['job_id']}", silent=True)
        if return_code == 0:
            print("Task cancelled.")
        else:
            print(f"Failed to cancel task (return code {return_code})")

def main_remote(func, conda_env, **kwargs):
    slurm_args = {
        "--gres": "gpu:1",
        "--mem": "8G",
        "--time": "01:00:00",
    }
    if "slurm_args" in kwargs:
        slurm_args.update(kwargs["slurm_args"])
    job_details = slurm_exec(
        func,
        job_name=kwargs.get("job_name", None),
        slurm_args = slurm_args,
        pre_run_commands = [
            f"conda activate {conda_env}",
        ],
        box_print = False
    )
    if job_details is not None:
        print(job_details)  # (*)

def is_chase_local():
    from socket import gethostname
    return "Chases-MacBook-Pro" in gethostname()

def main(func, is_local_exec=None, this_file=None, conda_env="neurotheory", **kwargs):
    if this_file is None:
        import inspect
        this_file = inspect.currentframe().f_back.f_locals["__file__"]
        print(f"Executing {__file__}#main, called from {this_file}")

    if is_local_exec is None:
        is_local_exec = is_chase_local()
    
    if is_local_exec:
        main_local(this_file, conda_env=conda_env, **kwargs)
    else:
        main_remote(func, conda_env=conda_env, **kwargs)
