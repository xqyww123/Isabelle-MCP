# Releasing isabelle-mcp

This adds to the SKILL conda-release (xqyww123/isabelle-packaging-ci, `.claude/skills/conda-release/SKILL.md`).

- Before tagging, `ci.yml` must also be green on the commit you tag: it runs `scripts/check_component.py`, the gate
  that catches a stale jar; the conda dry run does not run that gate.
- Never run `uv publish` (`CONTRIBUTING.md`, "Releasing", says why).
