from remoteexec.slurm import *

@slurm_job(job_name="test", pre_run_commands=["conda activate neurotheory"])
def test(name: str = "Chase"):
    print(f"Hello {name}!")
