# Versioning policy

agentic-analytics follows [Semantic Versioning](https://semver.org/). Before 1.0.0, a minor
release (0.x to 0.y) may break the public interface, and the changelog says how to migrate;
patch releases (0.x.y) never do.

## What is public

- **The command line**: the `agentic-analytics` and `aa` subcommands, their options, `--json`
  output and the exit codes.
- **The results format**: `summary.json` and `records.jsonl`, versioned by `schema_version`.
  A field is never renamed or removed within a major version of the format; new optional fields
  may appear in a minor release. Readers should ignore fields they do not know.
- **The dataset pack format**: `pack.yaml`, `questions.jsonl`, `probes.jsonl`, `parity.yaml` and
  `semantic.yaml`, as described in [writing a dataset pack](writing-a-dataset-pack.md) and
  [adding questions](adding-questions.md), versioned by their `schema_version`.
- **`run.yaml`**, the run configuration `init` writes.

Everything else, including the Python modules under `agentic_analytics`, is internal and may
change in any release.

## Packs and results

A pack has its own `version`; bump it whenever its questions, gold SQL or load SQL change.
Every result is stamped with the pack and version it ran on (`uk_price_paid@1.0`), the data
snapshot's sha256, the as-of date and the gold check id, so results from different data or
different gold are never compared by accident.

## Deprecation

A public command or option that is going away keeps working, with a warning, for at least one
minor release before it is removed, and the changelog names its replacement.

## Releases

A release is a `v*` git tag whose version matches `pyproject.toml`. The release workflow runs
the full checks, publishes to PyPI by trusted publishing (no stored token), then installs the
published version on Linux, macOS and Windows and runs the no-key commands against it.
