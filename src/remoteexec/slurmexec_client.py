import argparse
from pathlib import Path
from os.path import join as path_join
import ast
import sys
from typing import Optional
import subprocess
import inspect
from shlex import quote as _quote_cmdline_str

from .slurm import is_this_a_slurm_job, set_slurm_debug, SlurmJobMeta, SLURM_LOG_EOF_MESSAGE
from .utils import load_func_argparser

def load_module_from_file(path: Path):
    from importlib.util import spec_from_file_location, module_from_spec
    spec = spec_from_file_location(name=path.stem, location=str(path))
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def create_slurm_args(meta: SlurmJobMeta, unknown_args: Optional[list[str]] = None):
    slurm_args = {
        "--job-name": meta.job_name
    }
    slurm_args.update(meta.slurm_args)
    if unknown_args:
        i = 0
        while i < len(unknown_args):
            arg = unknown_args[i]
            if "=" in arg:
                key, value = arg.split("=", 1)
                slurm_args[key] = value
                i += 1
            else:
                if i + 1 >= len(unknown_args):
                    print(f"Unknown slurm argument '{arg}' without value. Supply either `--key value` or `--key=value`")
                    sys.exit(1)
                slurm_args[arg] = unknown_args[i+1]
                i += 2
        print(f"Passing `{' '.join(unknown_args)}` as arguments to SBATCH.")
    return slurm_args

def create_slurm_script(
    meta: SlurmJobMeta,
    slurm_args: dict[str, str],
    output_file: str,
    unknown_args: Optional[list[str]] = None,
):    
    # Set output file name as "{job id}_{array task id}"
    # %A is the slurm array parent job id
    # %a is the array task id
    # %x is the job name but we're saving that in the folder
    # %j is the job ID
    slurm_args["--output"] = slurm_args["--error"] = str(output_file)

    script_args_str = "\n".join([
        f"#SBATCH {arg}={value}" if arg.startswith("--")
        else f"#SBATCH {arg} {value}"
        for arg, value in slurm_args.items()
    ])

    exec_args_slurm = []
    # for argname, value in exec_args_dict.items():
    #     if isinstance(value, str):
    #         value = _quote_cmdline_str(value)
    #     exec_args_slurm.append(f"--{argname}={value}")
    # Now we are using the executed args:
    for arg in sys.argv[1:]:  # everything after the script name
        if arg not in unknown_args:  # ignore unk_args, which are assumed to be slurm arguments
            exec_args_slurm.append(_quote_cmdline_str(arg))

    exec_command = f"srun slurmexec {' '.join(exec_args_slurm)}"
    pre_run_commands_str = "\n".join(meta.pre_run_commands)
    

    script = f"""#!/bin/bash -l
# The -l flag makes the script run as if it were executed on the login node;
# this makes it so ~/.bashrc is loaded and the conda env loads properly.
#
# This script was created by slurmexec
#
{script_args_str}

echo "# Slurm job name: $SLURM_JOB_NAME"
echo "# Slurm node: $SLURM_JOB_NODELIST"
echo "# Slurm cluster: $SLURM_CLUSTER_NAME"
echo "# Slurm job id: $SLURM_JOB_ID"
echo "# Slurm array parent job id: $SLURM_ARRAY_JOB_ID"
echo "# Slurm array task id: $SLURM_ARRAY_TASK_ID"
echo "# Job start time: $(date)"
echo

{pre_run_commands_str}

echo "# Executing command: {exec_command}"
{exec_command}

echo "{SLURM_LOG_EOF_MESSAGE}"

# End of script
"""
    return script

def main():
    if len(sys.argv) == 1:
        print(f"Usage: slurmexec <filename.py[:function_name]> [args...]")
        sys.exit(0)
    
    filename = sys.argv[1]
    supplied_func_name = ":" in filename
    if supplied_func_name:
        filename, func_name = filename.split(":", 2)
    else:
        func_name = None
    path = Path(filename).resolve()

    if not path.exists():
        print(f"File '{path}' does not exist.")
        sys.exit(1)

    module = load_module_from_file(path)
    slurm_job_fns = {}

    for name in dir(module):
        func = getattr(module, name)
        if callable(func) and hasattr(func, "_slurm_job_meta"):
            slurm_job_fns[name] = func
    
    if func_name is None:
        if len(slurm_job_fns) == 1:
            func_name = next(iter(slurm_job_fns.keys()))
        else:
            print(f"Multiple slurm jobs found in the file ({', '.join(slurm_job_fns.keys())}). Please specify one with --func.")
            sys.exit(1)
    elif func_name not in slurm_job_fns:
        print(f"Function '{func_name}' does not exist or does not have @slurm_job. Available functions: {', '.join(slurm_job_fns.keys())}.")
        sys.exit(1)

    func = slurm_job_fns[func_name]
    meta = func._slurm_job_meta

    parser = load_func_argparser(func)
    # Create a more helpful usage string
    usage = parser.format_usage()  # "usage: slurmexec [...]"
    usage = usage[7:]  # remove "usage: "
    usage_split = usage.split(" ", 1)
    usage_split.insert(1, f"{sys.argv[1]}")  # insert the filename
    usage = " ".join(usage_split)
    parser.usage = usage
    # parser.usage = f"slurmexec {sys.argv[1]} [args...]"
    # parser.add_argument("--job_name", type=str, default=meta.name, help=f"Name of the slurm job (Defaults to function name, \"{meta.name}\")")
    # parser.add_argument("--local", action="store_true", help="Whether to run the job locally instead of on slurm.")
    exec_args, unknown_args = parser.parse_known_args(sys.argv[2:])  # ignore filename
    # job_name = exec_args.job_name
    # delattr(exec_args, "job_name")
    exec_args_dict = vars(exec_args)

    # print("[DEBUG] exec_args_dict:", exec_args_dict)
    # print("[DEBUG] unknown_args:", unknown_args)

    # If we are running on slurm already, execute function direction
    if is_this_a_slurm_job():
        func(**exec_args_dict)
        sys.exit(0)

    # Check if slurm is available
    try:
        subprocess.check_output(["slurmd", "-V"])  # this should just print version
    except Exception:
        # Slurm is not available, run locally
        print()
        print("*** Slurm not available; running job locally")
        if meta.pre_run_commands:
            print(f"*** Ignoring @slurm_job pre_run_commands: {meta.pre_run_commands}")
        if unknown_args:
            print(f"*** Ignoring Slurm command line args: {unknown_args}")
        print()
        
        # Refresh the module because is_this_a_slurm_job() will now return True
        set_slurm_debug(True, silent=True)
        module = load_module_from_file(path)
        func = getattr(module, func_name)
        func(**exec_args_dict)
        sys.exit(0)

    # Slurm exists, create a .slurm script and execute via sbash
    slurm_args = create_slurm_args(meta, unknown_args)
    is_array_task = "--array" in slurm_args or "-a" in slurm_args
    output_dir = Path.home() / "slurm_logs"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / ("%A_%a.out" if is_array_task else "%j.out")
    script = create_slurm_script(meta, slurm_args, output_file, unknown_args)
    script_dir = Path.cwd() / ".slurmexec"
    script_dir.mkdir(exist_ok=True)
    script_file = script_dir / f"{path.stem}__{func.__name__}.slurm"
    script_file.write_text(script)

    # Run sbatch script
    try:
        output = subprocess.check_output(["sbatch", str(script_file)], stderr=subprocess.STDOUT)
        output = output.decode().strip() # parse binary; strip newlines
    except subprocess.CalledProcessError as e:
        output = e.output.decode("utf-8")
    except Exception as e:
        raise RuntimeError(f"An unexpected error occurred: {e}")
    # print(f"[DEBUG] Slurm output: {output}")
    
    out_data = {
        "success": True,
        "message": output,
        "script_file": str(script_file),
        "is_array_task": is_array_task,
    }
    
    if output.startswith("Submitted batch job"):
        job_id = output.rsplit(" ", maxsplit=1)[-1] # last item
        log_file = slurm_args["--output"].replace("%x", slurm_args["--job-name"]).replace("%A", job_id).replace("%j", job_id)

        out_data["job_id"] = job_id
        out_data["log_file"] = log_file
        print(output)
        print(f"Script file: {script_file}")
        print(f"Log file: {log_file}")
    else:
        print("Failed to submit batch job:", output)
        print(f"Script file: {script_file}")
    
    print(out_data)
    

    # parser = argparse.ArgumentParser(description="Execute slurm job.")
    # parser.add_argument("filename", type=str, help="Python file (e.g., 'script.py')")
    # parser.add_argument("-f", "--func", type=str, default=None, required=False, help="Function in file, required if more than one @slurm_job")
    # parser.add_argument("--local", action="store_true", help="Run the executable locally instead of remotely.")
    # args, unknown_args = parser.parse_known_args()
    # slurm_jobs_in_file = parse_slurm_jobs_without_importing(path)

    # if len(slurm_jobs_in_file) == 0:
    #     print("No slurm jobs found in the file. Please annotate a function with @slurm_job.")
    #     sys.exit(1)
    
    # if args.func is None:
    #     if len(slurm_jobs_in_file) == 1:
    #         args.func = next(iter(slurm_jobs_in_file.keys()))
    #     else:
    #         print(f"Multiple slurm jobs found in the file ({', '.join(slurm_jobs_in_file.keys())}). Please specify one with --func.")
    #         sys.exit(1)
    # else:
    #     if args.func not in slurm_jobs_in_file:
    #         print(f"Function '{args.func}' not found in the file. Available functions: {', '.join(slurm_jobs_in_file.keys())}.")
    #         sys.exit(1)

    # job_kwargs = slurm_jobs_in_file[args.func]

    # # if args.local:
    # #     set_slurm_debug(True)
    # #     module = load_module_from_file(path)
    # #     func = getattr(module, args.func)
    # #     func(**job_kwargs)


    # print(f"Executing {args.func} with kwargs: {job_kwargs}")
    # print("Unknown args:", unknown_args)

    # module = load_module_from_file(path)
    # func = getattr(module, args.func)
    
    # slurm_exec(func)
