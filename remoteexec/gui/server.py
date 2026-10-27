"""FastAPI app for the job-monitor GUI."""
from __future__ import annotations

from pathlib import Path

from .service import JobService

STATIC_DIR = Path(__file__).parent / "static"


def create_app():
    try:
        from fastapi import FastAPI, Query
        from fastapi.responses import FileResponse, JSONResponse
        from fastapi.staticfiles import StaticFiles
    except ImportError as e:  # pragma: no cover
        raise SystemExit(
            "The GUI requires the [gui] extra: pip install 'remoteexec[gui]'"
        ) from e

    app = FastAPI(title="remoteexec")
    service = JobService()

    @app.middleware("http")
    async def no_store(request, call_next):
        """Never let the browser cache the page or its assets.

        The server is local and the files are a few KB, so caching buys
        nothing — but a stale app.js silently keeps running old client logic
        after an upgrade, which is very hard to diagnose from the UI.
        """
        resp = await call_next(request)
        if not request.url.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/jobs")
    def jobs(filters: str = Query("")):
        selected = {f for f in filters.split(",") if f}
        return {"jobs": service.list_jobs(selected or None)}

    @app.get("/api/jobs/{job_id}")
    def job_detail(job_id: str):
        d = service.detail(job_id)
        if d is None:
            return JSONResponse({"error": "unknown job"}, status_code=404)
        return d

    @app.get("/api/jobs/{job_id}/logs")
    def job_logs(job_id: str, offset: int = 0):
        return service.read_log(job_id, offset)

    @app.post("/api/jobs/{job_id}/cancel")
    def job_cancel(job_id: str):
        return service.cancel(job_id)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


def serve(host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True):
    import uvicorn

    if open_browser:
        import threading
        import webbrowser

        url = f"http://{host}:{port}/"
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(create_app(), host=host, port=port, log_level="warning")
