# Contributing

```bash
pip install -e ".[dev]"
pytest
python -m mypy src/
```

- Follow existing code style (Black 100 chars, Ruff)
- Add tests for new tools
- Run `pytest` before submitting PRs

---

# Releasing

**A release is a pushed tag.** Do not `uv publish` by hand: that skips the release gate
(`scripts/check_component.py`, which catches the stale jar you cannot see) and builds from your
working tree rather than from the commit you tagged.

```bash
# 1. Bump the version and finalise the changelog.
#    src/isabelle_mcp/__init__.py:  __version__ = "X.Y.Z"     (0.x: breaking -> bump the MINOR)
#    CHANGELOG.md:                  ## Unreleased  ->  ## X.Y.Z
git commit -am "Release X.Y.Z" && git push origin master      # let CI go green

# 2. Fire. Then approve the deployment on GitHub — nothing reaches PyPI until you do.
git tag -a vX.Y.Z -m "Release X.Y.Z" && git push origin vX.Y.Z
```
