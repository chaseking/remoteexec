import sys
import subprocess
from pathlib import Path
from functools import wraps
import inspect
import argparse
from shlex import quote as _quote_cmdline_str
from typing import Optional, List, Dict, NamedTuple
from types import SimpleNamespace

from .utils import get_env_var, load_func_argparser

__all__ = ["get_slurm_id", "is_this_a_slurm_job", "slurm_job", "slurm_exec", "set_slurm_debug"]

SLURM_LOG_EOF_MESSAGE = "# END OF SLURM JOB"

_IS_SLURM_DEBUG = False

def set_slurm_debug(debug: bool = True, silent: bool = False):
    """Sets slurm debug mode, allowing slurm jobs to be executed locally for debugging.

    Args:
        debug (bool, optional): Debug mode. Defaults to True.
    """
    global _IS_SLURM_DEBUG
    _IS_SLURM_DEBUG = debug

    if debug and not silent:
        print()
        print("=======================================================")
        print("|   NOTICE - Slurm running in debug mode.")
        print("|   All slurm tasks will be immediately executed")
        print("|   rather than queued on Slurm.")
        print("=======================================================")
        print()

def get_slurm_id():
    global _IS_SLURM_DEBUG
    if _IS_SLURM_DEBUG:
        return "SLURM_DEBUG"
    else:
        return get_env_var("SLURM_JOB_ID")

def is_this_a_slurm_job():
    global _IS_SLURM_DEBUG
    return get_env_var("SLURM_JOB_ID") is not None or _IS_SLURM_DEBUG


def parse_slurm_jobs_without_importing(path: Path) -> dict[str, dict[str, any]]:
    """
    Parse a Python file for slurm_job decorators.
    Returns a dictionary with function names as keys and their arguments as values.
    """
    import ast
    tree = ast.parse(path.read_text(), filename=str(path))
    slurm_jobs = {}

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            if node.decorator_list:
                for dec in node.decorator_list:
                    # Case 1: @slurm_job(**kwargs)
                    if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Name) and dec.func.id == "slurm_job":
                        slurm_jobs[node.name] = {
                            kw.arg: ast.literal_eval(kw.value)
                            for kw in dec.keywords
                        }
                    # Case 2: @slurm_job
                    elif isinstance(dec, ast.Name) and dec.id == "slurm_job":
                        slurm_jobs[node.name] = {}

    return slurm_jobs





class SlurmExecutableBuilder:
    """Class used to build a slurm executable command."""
    def __init__(self, job_name, full_job_name=None, script_dir=None):
        self.job_name = job_name
        self.full_job_name = full_job_name

        if script_dir is None:
            self._dir = Path.home() / "slurm"
        if not isinstance(script_dir, Path):
            self._dir = Path(script_dir).expanduser()
        else:
            self._dir = script_dir
        
        self.script_file = self._dir / job_name / f"_temp_job.slurm"
        
        self._args = {
            "--job-name": job_name,
        }
        self._commands = []

        self.output(f"%x_%j.log") # %x is job name, %A is job id assigned by slurm

        if full_job_name is None:
            self.command(f"echo '# Executing job \"{job_name}\" in a task generated using slurmexec.'")
        else:
            self.command(f"echo '# Executing job \"{job_name}\" ({full_job_name}) in a task generated using slurmexec.'")
    
    def arg(self, arg: str, value: any):
        self._args[arg] = value
        return self
    
    def args(self, args: dict[str, any]):
        for k, v in args.items():
            self._args[k] = v
        return self

    def output(self, filename):
        out = str(self._dir / filename)
        self.arg("--output", out)
        self.arg("--error", out)
        return self

    def is_array_task(self):
        return "--array" in self._args or "-a" in self._args

    def command(self, command: str | list[str]):
        if isinstance(command, str):
            self._commands.append(command)
        else:
            self._commands.extend(command)
        return self

    def sbatch(self, box_print=True):
        args = "\n".join([
            f"#SBATCH {arg}={value}" if arg.startswith("--")
            else f"#SBATCH {arg} {value}"
            for arg, value in self._args.items()
        ])
        commands = "\n".join(self._commands)

        script = f"""#!/bin/bash -l
# The -l flag makes the script run as if it were executed on the login node;
# this makes it so ~/.bashrc is loaded and the conda env loads properly.
#
# This script was created by slurmexec
#
{args}

{commands}

# End of script
"""
        
        # Write script to file
        self.script_file.parent.mkdir(parents=True, exist_ok=True)
        self.script_file.write_text(script)
        # with open(self.script_file, "w+") as file: # w+ creates if not exists, otherwise truncates
        #     file.write(script)
        
        # Execute file
        try:
            output = subprocess.check_output(["sbatch", str(self.script_file)], stderr=subprocess.STDOUT)
            output = output.decode().strip() # parse binary; strip newlines
        except subprocess.CalledProcessError as e:
            output = e.output.decode("utf-8")
        except Exception as e:
            raise RuntimeError(f"An unexpected error occurred: {e}")

        def bprint(*args, **kwargs):
            if box_print:
                print("|  ", *args, **kwargs)
            else:
                print(*args, **kwargs)

        if box_print:
            print()
            print("*===============================================================================*")
        
        bprint(f"Executing Slurm job with name \"{self.job_name}\"...")
        if self.full_job_name is not None:
            bprint(f"   ({self.full_job_name})")
        bprint("")

        out_data = {
            "success": True,
            "script_file": str(self.script_file),
            "is_array_task": self.is_array_task(),
        }

        bprint(f"Slurm output: {output}")
        
        if output.startswith("Submitted batch job"):
            job_id = output.rsplit(" ", maxsplit=1)[-1] # last item
            log_file = self._args["--output"].replace("%x", self.job_name).replace("%A", job_id).replace("%j", job_id)

            out_data["job_id"] = job_id
            out_data["log_file"] = log_file
            bprint("Status: SUCCESS")
            bprint(f"Slurm job id: {job_id}")
            bprint(f"Script file: {self.script_file}")
            bprint(f"Log file: {log_file}")
        else:
            out_data["success"] = False
            bprint("Status: FAIL [!!!]")
            bprint(f"Script file: {self.script_file}")
            bprint(f"Error:")
            
            for line in output.split("\n"):
                bprint(f"{line}")
        
        if box_print:
            print("|")
            print("*===============================================================================*")
            print()

        return out_data
        
class SlurmJobMeta(NamedTuple):
    job_name: Optional[str] = None
    slurm_args: dict[str, any] = {}
    pre_run_commands: list[str] = []

def slurm_job(
    job_name: Optional[str] = None,
    conda_env: Optional[str] = None,
    slurm_args: dict[str, any] = {},
    pre_run_commands: list[str] = [],
    **other_slurm_args
):
    """
    Function decorator to be applied to a function representing a Slurm job.
    """
    for k, v in other_slurm_args.items():
        slurm_args[f"--{k}"] = v
    if conda_env is not None:
        pre_run_commands.insert(0, f"conda activate {conda_env}")
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            if is_this_a_slurm_job():
                return func(*args, **kwargs)
            else:
                raise RuntimeError(f"Function {func.__name__} cannot be run outside of a slurm job. Please use slurm_exec() to run this job.")
        wrapper._is_slurm_job = True # Tags the function as a slurm job

        meta = SlurmJobMeta(
            job_name = (job_name or func.__name__),
            slurm_args = slurm_args,
            pre_run_commands = pre_run_commands
        )
        # print("Got unknown meta kwargs:", meta_kwargs)
        wrapper._slurm_job_meta = meta
        return wrapper
    return decorator


def slurm_exec(
    func: callable,
    argparser: Optional[argparse.ArgumentParser] = None,
    script_dir: str = "~/slurm",
    job_name: Optional[str] = None,
    slurm_args: Optional[Dict[str, any]] = None,
    pre_run_commands: Optional[List[str]] = None,
    srun: bool = True,
    **kwargs
):
    """Runs a slurm job. Used in the main method of a .py file.
    Specifically, if called from within a slurm task, `func` will be called.
    Otherwise, it creates a new slurm task specified by the arguments.

    Args:
        func (callable): Function to call
        argparser (argparse.ArgumentParser, optional): Argument parser for the function. Defaults to parsing the arguments in the `func` declaration.
        script_dir (str, optional): Script directory. Defaults to ~/slurm.
        slurm_args (dict, optional): Slurm batch arguments. Defaults to {}.
        pre_run_commands (list, optional): List of commands to run before the main command (e.g., activate environment). None by default.
        srun (bool, optional): Whether to use srun to execute the python command (i.e., `srun python myfile.py --args`). Defaults to True.

    Raises:
        ValueError: If func is not an @slurm_job
    """
    if not hasattr(func, "_is_slurm_job"):
        raise ValueError(f"Function {func.__name__} must be decorated with @slurm_job")
    
    if slurm_args is None:
        slurm_args = {}
    if pre_run_commands is None:
        pre_run_commands = []
    
    if job_name is None:
        job_name = func.__name__
    script_dir = script_dir.format(job_name=job_name)
    
    # First we check if this function was called from a slurm task
    # This is identified by whether a slurm-id argument is passed
    given_argparser = argparser is not None
    if given_argparser:
        if isinstance(argparser, argparse.ArgumentParser):
            # parser = argparse.ArgumentParser(parents=[argparser])
            parser = argparser
        elif callable(argparser):
            parser = argparser()
        else:
            raise ValueError("argparser must be an instance of argparse.ArgumentParser or a callable that returns an argparse.ArgumentParser")
    else:
        parser = load_func_argparser(func)

    parser.add_argument("--job_name", type=str, default=job_name, help=f"Name of the slurm job. (Default: \"{job_name}\")")
    exec_args, unk_args = parser.parse_known_args()
    job_name = exec_args.job_name
    delattr(exec_args, "job_name")
    exec_args_dict = vars(exec_args)
    
    if is_this_a_slurm_job():
        # This was executed from within a slurm job; call function directly
        if given_argparser:
            # If an argparser was given, then the function expects a single argument of the parsed args
            func(exec_args)
        else:
            # Otherwise, pass each argument as a keyword argument
            func(**exec_args_dict)
        return None
    else:
        # This function was executed by a user, with the intention to start a slurm task
        # Parse function arguments and use these as arguments when executing the task
        # Load slurm batch arguments
        default_slurm_args = {
            # "--ntasks": 1,
            # "--cpus-per-task": 1,
            # "--mem-per-cpu": "1G"
            # "--time": "1-00:00:00",  # default 1 day
        }

        # slurm_args = {(f"--{k}" if not k.startswith("--") else k): v for k, v in slurm_args.items()}
        slurm_args = default_slurm_args | slurm_args

        # Pass any unknown command line arguments to slurm_args
        if unk_args:
            # Note these must be formatted as "--key1 value1 --key2 value2..."
            slurm_unk_args = {
                unk_args[i]: unk_args[i+1]
                for i in range(0, len(unk_args), 2)
            }
            print(f"Passing `{' '.join(unk_args)}` as arguments to SBATCH.")
            slurm_args = slurm_args | slurm_unk_args

        # Load job name: "{filename}-{func_name}"
        func_file = Path(inspect.getfile(func.__wrapped__))  # the file of the function (wrapped used because otherwise it would return the decorator file)
        full_job_name = f"{func.__name__}() in {func_file}"
        
        if job_name is None:
            func_filename = func_file.name
            func_filename = func_filename[:func_filename.rindex(".")]
            job_name = f"{func_filename}-{func.__name__}"
        
        job_name = job_name.format(**exec_args_dict)

        slurm = SlurmExecutableBuilder(job_name, full_job_name=full_job_name, script_dir=script_dir)
        slurm.args(slurm_args)
        is_array_task = slurm.is_array_task()

        # Set output file name as "{job id}_{array task id}"
        # %A is the slurm array parent job id
        # %a is the array task id
        # %x is the job name but we're saving that in the folder
        # %j is the job ID
        slurm.output("%A_%a.out" if is_array_task else "%j.out")
        
        slurm.command([
            # Print some info about the cluster
            'echo "# Slurm job name: $SLURM_JOB_NAME"',
            'echo "# Slurm node: $SLURM_JOB_NODELIST"',
            'echo "# Slurm cluster: $SLURM_CLUSTER_NAME"',
            'echo "# Slurm job id: $SLURM_JOB_ID"'
        ])
        
        if is_array_task:
            slurm.command([
                'echo "# Slurm array parent job id: $SLURM_ARRAY_JOB_ID"',
                'echo "# Slurm array task id: $SLURM_ARRAY_TASK_ID"',
            ])
        
        slurm.command([
            'echo "# Job start time: $(date)"',
            'echo'
        ])

        slurm.command(pre_run_commands)
        
        python_file = str(func_file)
        exec_args_slurm = []
        # for argname, value in exec_args_dict.items():
        #     if isinstance(value, str):
        #         value = _quote_cmdline_str(value)
        #     exec_args_slurm.append(f"--{argname}={value}")
        # Now we are using the executed args:
        for arg in sys.argv[1:]:  # everything after the script name
            if arg not in unk_args:  # ignore unk_args, which are assumed to be slurm arguments
                exec_args_slurm.append(_quote_cmdline_str(arg))
        
        command = ["python", python_file]
        command.extend(exec_args_slurm)
        if srun:
            command.insert(0, "srun")
        command = " ".join(command)

        slurm.command(f"echo '# Executing via:' {command}")
        slurm.command("echo")
        slurm.command(command)

        # Finally execute the sbatch
        return slurm.sbatch(**kwargs)
