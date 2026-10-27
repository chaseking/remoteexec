# remoteexec menu bar app

A macOS (13+) menu bar app for remoteexec jobs:

- **Dropdown** — running jobs and jobs that finished in the last 6 hours (name, job id,
  host, elapsed time / how long ago). The menu bar shows how many are running or queued.
- **Click a job** → the jobs window (the same UI as `remoteexec gui`): a sidebar with the
  same job list plus search and a finished-window filter, and the selected job's live log
  (with a live / complete indicator), output files with previews and *Download outputs*,
  and a Details tab (paths, exit code, the submitted sbatch script). ↑/↓ move between jobs.
- **Host switcher** — the `axon ▾` menu routes a cluster through another ssh alias
  (e.g. `axon-remote` off-site, or *Automatic*); *Manage clusters & ssh hosts…* adds or
  edits them. The CLI and Python API follow the same setting.
- Notifications when a job finishes (click one to open it).

It is a thin native shell around the local `remoteexec gui` server: it reuses one already
running on the port (default 8765) or starts it through your login shell and stops it on
quit, so `remoteexec` must be installed (`pip install -e .` / `uv tool install -e .`).

## Build

Needs Xcode or the Command Line Tools (`xcode-select --install`).

```bash
cd macos
./build.sh             # -> build/remoteexec.app   (open build/remoteexec.app)
./build.sh --install   # copy to /Applications and launch
```

To start it at login: System Settings → General → Login Items → add `remoteexec`.
`open remoteexec.app --args --open-dashboard` also opens the jobs window at launch.

Working on the UI without a cluster: `python tests/demo_dashboard.py` serves the dashboard
on port 8765 with sample jobs from a fake Slurm; the app picks it up.

## Settings

*Settings…* in the dropdown: the command used to launch the CLI (empty = auto-detect
`~/.local/bin`, Homebrew and conda locations, then your login shell's `PATH`; or e.g.
`~/miniconda3/bin/remoteexec`), the port, and notifications. Backend output goes to
`~/Library/Logs/remoteexec-menubar.log`.

## Icon

The menu bar icon is the SF Symbol `terminal` (`terminal.fill` while jobs run): change
`iconIdle` / `iconBusy` in `Sources/RemoteExecBar/App.swift`. For an app icon, put an
`AppIcon.icns` next to `build.sh`; it is picked up automatically.
