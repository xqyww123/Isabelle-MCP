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

**A release is a pushed tag**, made by the SKILL conda-release (xqyww123/isabelle-packaging-ci)
and this repository's [RELEASE.md](RELEASE.md). Do not `uv publish` by hand: that skips the
release gate (`scripts/check_component.py`, which catches the stale jar you cannot see) and
builds from your working tree rather than from the commit you tagged.
