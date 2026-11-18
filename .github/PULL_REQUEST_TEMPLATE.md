## What this changes and why

## How it was tested

- [ ] `uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest -q -m "not live"`
- [ ] New behaviour and its error cases have tests; no test makes a paid call
- [ ] Scorer or pipeline output changed? Its code version is bumped ([versioning](https://github.com/biplob819/agentic-analytics/blob/main/docs/versioning.md))
- [ ] Pack or questions changed? `agentic-analytics pack check` passes and the pack version is bumped
- [ ] No hand-typed result numbers; figures come from `summary.json`
- [ ] `changelog.md` has a line
- [ ] No keys, `.env` contents or private data anywhere in the diff
