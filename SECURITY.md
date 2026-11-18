# Security

## Reporting a vulnerability

Please report security problems privately through GitHub's
[private vulnerability reporting](https://github.com/biplob819/agentic-analytics/security/advisories/new)
for this repository, not in a public issue. Include what you found, how to reproduce it, and the
version you tested. Reports are acknowledged as soon as possible; once a fix is released, the
advisory is published with credit to you unless you prefer otherwise.

If private vulnerability reporting is not available to you, [open a public issue](https://github.com/biplob819/agentic-analytics/issues/new)
titled **"Security contact request"** with no details of the problem at all. The maintainer
replies on the issue with a private channel to send the report through, and the issue is closed
without discussing the problem in public.

Supported versions: the latest release.

## How keys are handled

- Keys and passwords are read from environment variables or a local `.env` file;
  `.env.example` lists every variable. `.env`, `*.p8` and `*.pem` are git-ignored.
- Each key is sent only to the service it belongs to, through that service's official client.
- The tool never prints, logs or writes a key value. `doctor` reports which variables are set by
  name only. Exported traces and Langfuse spans pass through a redactor that removes secret
  values, auth headers and known key patterns. `scripts/secret_scan.py` runs in CI.
- Snowflake users authenticate with key pairs; keep the private key file outside the repository.
- Admin credentials (`*_ADMIN_*`) are used only for loading and role setup. Runs and any hosted
  demo use the read-only credentials alone.

## Read-only by design

An agent writes SQL that runs on your warehouse, so every query passes four layers:

1. **SQL guard.** One `SELECT` statement only: no writes, no DDL, no table functions or other
   functions that reach the network or the file system, no `SETTINGS`, `FORMAT` or `INTO`.
2. **ClickHouse role** (`analytics_ro`) with `SELECT` on the pack's databases only and no source
   grants, under a profile with read-only settings, a row cap and a timeout.
3. **Snowflake role** (`ANALYTICS_RO`) that can only select, on a small warehouse that
   auto-suspends.
4. **MCP tool allow-list.** Only the query and schema tools are exposed to the agent;
   control-plane tools are never offered.

`doctor` checks the roles are read-only before anything runs. Write and injection probes in
every pack test that each backend refuses, and a run fails if the data's fingerprint changes.

## What to keep out of a public repository

Your `.env`, private keys, `.cache/`, and `results/` if your data or questions are private.
