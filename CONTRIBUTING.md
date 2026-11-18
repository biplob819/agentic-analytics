# Contributing to agentic-analytics

Thanks for helping. Bug reports, dataset packs, questions, fixes and documentation are all
welcome.

## Set up

```bash
git clone https://github.com/biplob819/agentic-analytics
cd agentic-analytics
uv sync
uv run pytest -q -m "not live"
```

The test suite spends nothing and makes no network calls, with one exception: the test marked
`build` runs a real `uv build`, which fetches the build backend (hatchling) from PyPI unless uv
has it cached. Models, MCP, Cortex and warehouses are faked or replayed from recordings. You do not need keys or accounts to contribute. Tests
marked `live` need real accounts and never run in CI.

## Before you open a pull request

```bash
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest -q -m "not live"
uv run python scripts/secret_scan.py
```

CI runs the same checks on Python 3.11 to 3.13 on Linux, macOS and Windows.

- **Tests first.** A bug fix comes with a test that fails without it; a feature comes with
  tests for its behaviour and its error cases.
- **Never make a paid call from a test.** Use the fakes under `tests/`; `run` in tests is
  always `--dry-run`, `--rescore` or faked.
- **Keys never appear in code, tests, logs or output.**
- **Text files are UTF-8**, and every `open`, `read_text` and `write_text` names its encoding (a
  test enforces this, for Windows).
- **No hand-typed results.** Figures in the README and write-ups are generated from a run's
  `summary.json` (`scripts/publish_results.py`); a test fails on result numbers typed outside
  the generated block.
- **Changing what a scorer or pipeline step outputs?** Bump its code version so cached answers
  are not mixed (see [docs/versioning.md](docs/versioning.md)).
- Add a line to `changelog.md`.

## Adding a dataset pack or questions

See [writing a dataset pack](docs/writing-a-dataset-pack.md) and
[adding questions](docs/adding-questions.md). A pack's data needs a licence that allows
redistribution and evaluation use, and its attribution goes in `pack.yaml` word for word.
`agentic-analytics pack check --pack PATH` must pass with no issues.

## Proposing a larger change

Open an issue first describing the problem and the change you have in mind, so we can agree on
the approach before you invest the time. Design decisions are recorded in
[docs/decisions.md](docs/decisions.md).

By contributing you agree that your work is released under the project's MIT licence, and to
follow the [code of conduct](CODE_OF_CONDUCT.md).
