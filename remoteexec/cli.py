"""remoteexec CLI — §19 (optional [cli] extra)."""
from __future__ import annotations

try:
    import typer
except ImportError:  # pragma: no cover
    raise SystemExit("CLI requires the [cli] extra: pip install 'remoteexec[cli]'")

from . import state
from .clusters.slurm import SlurmCluster

app = typer.Typer(help="remoteexec — remote Slurm execution")


def _cluster_for(job_id: str) -> tuple[SlurmCluster, dict]:
    rec = state.get(job_id)
    if rec is None:
        raise typer.Exit(f"no tracked job {job_id}")
    return SlurmCluster(host=rec["cluster_host"]), rec


@app.command()
def connect(cluster: str):
    """Warm the ControlMaster (no-op for key-only axon)."""
    SlurmCluster(host=cluster).connect()
    typer.echo(f"connected to {cluster}")


@app.command()
def gui(
    port: int = typer.Option(8765, help="Port to serve on"),
    host: str = typer.Option("127.0.0.1", help="Address to bind (local only)"),
    no_browser: bool = typer.Option(False, "--no-browser", help="Don't open a browser"),
):
    """Launch the local web GUI for monitoring jobs."""
    from .gui.server import serve

    typer.echo(f"remoteexec GUI -> http://{host}:{port}/  (Ctrl-C to stop)")
    serve(host=host, port=port, open_browser=not no_browser)


@app.command()
def logs(job_id: str, follow: bool = typer.Option(False, "-f", "--follow")):
    c, _ = _cluster_for(job_id)
    job = c.job(job_id)
    job.stream(follow=follow)


@app.command()
def status(job_id: str = typer.Argument(None)):
    if job_id:
        c, _ = _cluster_for(job_id)
        typer.echo(f"{job_id}: {c.status(job_id)}")
    else:
        for jid, rec in state.all_jobs().items():
            c = SlurmCluster(host=rec["cluster_host"])
            typer.echo(f"{jid}\t{rec['name']}\t{c.status(jid)}")


@app.command()
def fetch(job_id: str, remote: str, local: str = typer.Argument(None)):
    c, _ = _cluster_for(job_id)
    job = c.job(job_id)
    typer.echo(str(job.fetch(remote, local)))


@app.command()
def cancel(job_id: str):
    c, _ = _cluster_for(job_id)
    c.cancel(job_id)
    typer.echo(f"cancelled {job_id}")


@app.command()
def ls():
    for jid, rec in state.all_jobs().items():
        typer.echo(f"{jid}\t{rec['name']}\t{rec['submitted_at']}\t{rec['workdir']}")


if __name__ == "__main__":
    app()
