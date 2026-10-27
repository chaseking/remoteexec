"""Offline unit tests — no cluster needed (§23)."""
from __future__ import annotations

from remoteexec.clusters.slurm import env_problems
from remoteexec.environment import ApptainerEnv, CondaEnv, UvEnv, env_label
from remoteexec.paths import rq
from remoteexec.resources import SlurmResources
from remoteexec.templates import render_sbatch


def test_rq_passthrough_and_quoting():
    assert rq("~/x") == "~/x"
    assert rq("/abs/p") == "/abs/p"
    assert rq("$SCRATCH/data") == "$SCRATCH/data"
    # spaces must be protected
    q = rq("$SCRATCH/a b")
    assert q.startswith('"') and q.endswith('"')
    assert "a b" in q


def test_resource_merge_layers():
    base = SlurmResources(time="04:00:00", cpus_per_task=8, mem="32G")
    deco = SlurmResources(gpus=1, mem="48G")
    eff = base.merge(deco)
    assert eff.time == "04:00:00"  # from base
    assert eff.cpus_per_task == 8  # from base
    assert eff.mem == "48G"  # decorator overrides
    assert eff.gpus == 1


def test_sbatch_lines_gpu_and_name():
    res = SlurmResources(gpus="a100:2", partition="ctn", job_name="train")
    lines = res.to_sbatch_lines()
    assert "#SBATCH --gres=gpu:a100:2" in lines
    assert "#SBATCH --partition=ctn" in lines
    assert "#SBATCH --job-name=train" in lines


def test_gres_emitted_once():
    # regression: gres was previously added twice in to_sbatch_lines
    lines = SlurmResources(partition="gpu", gres="gpu:1").to_sbatch_lines()
    assert lines.count("#SBATCH --gres=gpu:1") == 1


def test_apptainer_env_activation_and_launcher():
    env = ApptainerEnv("/share/singularity/jax_cuda.sif")
    act = env.activation_script()
    assert "module purge" in act
    assert ("export APPTAINERENV_PYTHONPATH="
            "$HOME/.local/lib/python3.12/site-packages") in act
    assert env.launcher() == (
        "apptainer exec --nv /share/singularity/jax_cuda.sif python")
    assert env_label(env) == "apptainer:jax_cuda.sif"


def test_render_sbatch_apptainer_jax_gpu():
    # the axon-jax-gpu execution shape: gpu partition + --gres, module purge,
    # APPTAINERENV_PYTHONPATH export, and an `apptainer exec --nv ... python` run line.
    script = render_sbatch(
        SlurmResources(partition="gpu", gres="gpu:1"),
        env=ApptainerEnv("/share/singularity/jax_cuda.sif"),
        setup=[], cd_dir="/x", logdir="/x/logs",
        runner_path="/x/_runner.py",
        payload_path="p.pkl", result_path="r.pkl",
    )
    assert "#SBATCH --partition=gpu" in script
    assert script.count("#SBATCH --gres=gpu:1") == 1
    assert "module purge" in script
    assert ("export APPTAINERENV_PYTHONPATH="
            "$HOME/.local/lib/python3.12/site-packages") in script
    assert ("apptainer exec --nv /share/singularity/jax_cuda.sif python "
            "/x/_runner.py p.pkl r.pkl") in script


def test_axon_jax_gpu_remote_host_selection():
    from remoteexec.clusters.presets import AxonJaxGpuCluster

    assert AxonJaxGpuCluster().conn.host == "axon"
    assert AxonJaxGpuCluster(remote=False).conn.host == "axon"
    assert AxonJaxGpuCluster(remote=True).conn.host == "axon-remote"
    # explicit host override still wins
    assert AxonJaxGpuCluster(remote=True, host="my-axon").conn.host == "my-axon"


def test_conda_activation_with_init():
    env = CondaEnv("base", pre=["module load cuda"], init=True)
    script = env.activation_script()
    assert "module load cuda" in script
    assert 'eval "$(conda shell.bash hook)"' in script
    assert "conda activate base" in script
    assert env.launcher() == "python"


def test_uv_launcher_includes_cloudpickle():
    assert "--with cloudpickle" in UvEnv().launcher()
    assert UvEnv().launcher().startswith("uv run")


def test_env_problems():
    # identical -> no problems
    assert env_problems(("3.11", "3.1.2"), ("3.11", "3.1.2")) == []
    # cloudpickle patch/minor differences within the same major are fine
    assert env_problems(("3.11", "3.0.0"), ("3.11", "3.1.2")) == []
    # python minor mismatch -> flagged
    probs = env_problems(("3.12", "3.1.2"), ("3.11", "3.1.2"))
    assert len(probs) == 1 and "Python" in probs[0]
    # cloudpickle major mismatch -> flagged
    probs = env_problems(("3.11", "3.1.2"), ("3.11", "2.2.1"))
    assert len(probs) == 1 and "cloudpickle" in probs[0]
    # both mismatched -> two problems
    assert len(env_problems(("3.12", "3.1.2"), ("3.11", "2.2.1"))) == 2


def test_render_sbatch_structure():
    # shared workspace as cwd; per-run dir (.rx/<session>) for runner/result/logs
    ws = "/home/u/remoteexec/proj"
    rd = f"{ws}/.rx/20260618-1000-abc123"
    res = SlurmResources(partition="ctn", time="00:05:00", job_name="countdown")
    script = render_sbatch(
        res, env=CondaEnv("base", init=True), setup=["export X=1"],
        cd_dir=ws, logdir=f"{rd}/logs",
        runner_path=f"{rd}/_runner.py",
        payload_path=f"{rd}/payload.pkl",
        result_path=f"{rd}/result.pkl",
    )
    assert script.startswith("#!/bin/bash")
    assert f"#SBATCH --output={rd}/logs/%j.out" in script
    assert "export X=1" in script
    # cwd is the shared workspace (relative reads/imports behave like project root)
    assert f"cd {ws}" in script
    assert f'export PYTHONPATH="{ws}:' in script
    assert "export REMOTEEXEC_REMOTE=1" in script
    # runner + result resolved under the per-run .rx dir, by absolute path
    assert f"python {rd}/_runner.py {rd}/payload.pkl {rd}/result.pkl" in script
    assert "set -euo pipefail" in script


def test_render_sbatch_array():
    ws = "/home/u/remoteexec/proj"
    rd = f"{ws}/.rx/sess"
    script = render_sbatch(
        SlurmResources(partition="ctn", array="0-2", job_name="sweep"),
        env=CondaEnv("base"), setup=[], cd_dir=ws, logdir=f"{rd}/logs",
        runner_path=f"{rd}/_runner.py", array_dir=rd,
    )
    assert f"cd {ws}" in script
    assert f"{rd}/payloads/${{SLURM_ARRAY_TASK_ID}}.pkl" in script
    assert f"{rd}/results/${{SLURM_ARRAY_TASK_ID}}.pkl" in script
