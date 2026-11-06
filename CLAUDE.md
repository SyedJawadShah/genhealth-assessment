@AGENTS.md

## Notes for Claude Code

- Run `make check` before reporting a change as done, and include the result.
- Prefer editing existing modules over adding new ones.
- Add dependencies when the task needs them, using `uv add` (or `uv add --dev` for tooling such
  as type stubs), and say why in the commit message. Ask before changing CI workflows or
  editing deploy scripts.
