"""sbatch script rendering — §13. Single combined log."""
from __future__ import annotations

from .resources import SlurmResources

SBATCH_TEMPLATE = """#!/bin/bash
{sbatch_directives}
#SBATCH --output={logdir}/%j.out
# (no --error: stderr is merged into the same file -> everything in one log)

set -euo pipefail
echo "[remoteexec] host=$(hostname) job=${{SLURM_JOB_ID}} start=$(date -Is) dir=$(pwd) cd={cd_dir}"

{setup}
{env_activation}

cd {cd_dir}
export PYTHONPATH="{cd_dir}:${{PYTHONPATH:-}}"
export REMOTEEXEC_REMOTE=1

rc=0
{run_line}
echo "[remoteexec] end=$(date -Is) rc=${{rc}}"
exit ${{rc}}
"""


def render_sbatch(
    resources: SlurmResources,
    *,
    env,
    setup,
    cd_dir: str,
    logdir: str,
    runner_path: str,
    payload_path: str = "",
    result_path: str = "",
    array_dir: str | None = None,
) -> str:
    """Render the sbatch script.

    ``cd_dir`` is the shared project workspace (cwd + PYTHONPATH) so the
    function's relative paths/imports behave like the project root. The runner
    is invoked by its absolute ``runner_path`` (independent of cwd, §8.1).
    For job arrays, pass ``array_dir`` (the per-run dir holding
    ``payloads/<i>.pkl`` and ``results/<i>.pkl``).
    """
    directives = "\n".join(resources.to_sbatch_lines())
    launcher = env.launcher()
    if array_dir is not None:
        run_line = (
            f"{launcher} {runner_path} "
            f"{array_dir}/payloads/${{SLURM_ARRAY_TASK_ID}}.pkl "
            f"{array_dir}/results/${{SLURM_ARRAY_TASK_ID}}.pkl || rc=$?"
        )
    else:
        run_line = f"{launcher} {runner_path} {payload_path} {result_path} || rc=$?"
    return SBATCH_TEMPLATE.format(
        sbatch_directives=directives,
        setup="\n".join(setup),
        env_activation=env.activation_script(),
        cd_dir=cd_dir,
        logdir=logdir,
        run_line=run_line,
    )
