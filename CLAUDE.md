# Working on remoteexec

## Versioning

Versions are `release.major.minor` (e.g. `0.3.2`: release 0, major 3, minor 2).

- Unless told otherwise, **every change bumps the minor version** (`0.3.2` → `0.3.3`).
- Bump major only for a significant new feature set or a breaking change, and release only
  when asked; either resets the parts after it to 0.
- Change it in all three places, which must match: `version` in `pyproject.toml`,
  `__version__` in `remoteexec/serialize.py`, and the `remoteexec` package entry in `uv.lock`.
- Why it matters: the macOS app only offers an update when the version on `main` is higher
  than the installed one (see `macos/README.md` → Updates).

## Merging and branches

- When a change is finished (tests pass, version bumped), **merge it into `main` and push
  `main` without waiting to be asked**. The app's updater follows `main`, so work left on a
  side branch never reaches the installed app.
- Fast-forward or use a merge commit; never rewrite `main`'s history. If `main` has moved
  on, merge it into your branch first and re-run the tests.
- **Delete the branch once it is merged**, on the remote as well as locally
  (`git push origin --delete <branch>`, `git branch -d <branch>`). Before deleting, check
  with `git merge-base --is-ancestor <branch> origin/main` that nothing on it is unmerged.
  For more work later, start a fresh branch from `main`.

## Tests

```bash
uv run --python 3.11 --with pytest --with cloudpickle python -m pytest tests/ -q
```

The macOS app (`macos/`) only builds on macOS; the `macos-app` GitHub workflow builds and
launches it on every push that touches `macos/` or `remoteexec/gui/`.
