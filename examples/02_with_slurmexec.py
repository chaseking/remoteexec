from slurmexec import *

if is_this_a_slurm_job():
    import time

@slurm_job
def countdown(start: int = 10):
    print(f"Starting countdown task with slurm ID: {get_slurm_id()}")
    print("hostname:", gethostname())

    ticker = start
    
    while ticker > 0:
        print(f"{ticker}...")
        time.sleep(1)
        ticker -= 1
    
    print("Done!")

if __name__ == "__main__":
    from socket import gethostname
    is_local_host = "Chases-MacBook-Pro" in gethostname()

    if is_local_host:
        # Copy to remote host and execute
        import sys
        from pathlib import Path
        from remoteexec import *

        to_copy = Path(__file__)
        remote = "axon"
        remote_dir = "~/TEST/"
        rsync(
            src = to_copy,
            dst = f"{remote}:~/TEST",
        )
        ssh_exec_cd_and_python(
            remote = remote,
            dirname = remote_dir,
            filename = to_copy.name,
            args = sys.argv[1:],
            title = f"Running python script {to_copy.name} on {remote}",
        )
    else:
        # We are on remote host; run slurm job
        slurm_exec(
            func = countdown,
            job_name = "my_countdown_task",  # if not supplied the function name is used (here "countdown")
            slurm_args = {
                "--partition": "ctn",
                "--time": "0-00:01:00",  # default 1 min runtime
            },
            pre_run_commands = [
                "conda activate jaxley",
            ]
        )