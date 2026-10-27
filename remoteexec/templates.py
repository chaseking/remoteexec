"""sbatch script rendering. stdout+stderr go to one combined log."""
from __future__ import annotations

import posixpath

from .paths import rq
from .resources import SlurmResources

SBATCH_TEMPLATE = """#!/bin/bash
{sbatch_directives}
#SBATCH --output={logdir}/%j.out

set -euo pipefail
echo "[remoteexec] host=$(hostname) job=${{SLURM_JOB_ID}} start=$(date +%Y-%m-%dT%H:%M:%S%z)"

{setup}
{env_activation}

cd {cd_dir}
export PYTHONPATH="{pythonpath}:${{PYTHONPATH:-}}"
export REMOTEEXEC_REMOTE=1 PYTHONUNBUFFERED=1
export REMOTEEXEC_OUTPUT_DIR="{outdir}"
mkdir -p "$REMOTEEXEC_OUTPUT_DIR"

rc=0
{run_line}
echo "[remoteexec] end=$(date +%Y-%m-%dT%H:%M:%S%z) rc=${{rc}}"
exit ${{rc}}
"""

TASK = "${SLURM_ARRAY_TASK_ID}"


def render_sbatch(
    resources: SlurmResources,
    *,
    env,
    setup,
    cd_dir: str,
    logdir: str,
    runner_path: str = "",
    payload_path: str = "",
    result_path: str = "",
    array_dir: str | None = None,
    outdir: str | None = None,
    command: str | None = None,
    pythonpath: str | None = None,
) -> str:
    """Render the sbatch script.

    The job runs from ``cd_dir`` (the synced workspace, or the mirror of the
    local cwd), with the runner invoked by absolute path. Array jobs pass
    ``array_dir`` holding ``payloads/<i>.pkl`` / ``results/<i>.pkl``.
    ``command`` makes it a plain shell-command job instead (no runner).
    """
    rundir = posixpath.dirname(logdir)
    if outdir is None:
        outdir = f"{rundir}/out" + (f"/{TASK}" if array_dir is not None else "")
    if command is not None:
        # The user's command runs in this shell (so `module`, conda etc. work)
        # without -e/-u, so its own exit code is what the job reports.
        run_line = f"set +eu\n{command}\nrc=$?"
    elif array_dir is not None:
        run_line = (
            f"{env.launcher()} {runner_path} "
            f"{array_dir}/payloads/{TASK}.pkl {array_dir}/results/{TASK}.pkl || rc=$?"
        )
    else:
        run_line = f"{env.launcher()} {runner_path} {payload_path} {result_path} || rc=$?"
    return SBATCH_TEMPLATE.format(
        sbatch_directives="\n".join(resources.to_sbatch_lines()),
        setup="\n".join(setup),
        env_activation=env.activation_script(),
        cd_dir=rq(cd_dir),
        pythonpath=pythonpath or cd_dir,
        outdir=outdir,
        logdir=logdir,
        run_line=run_line,
    )
