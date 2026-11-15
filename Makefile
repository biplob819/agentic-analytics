# Flow A targets (PRD). Each wraps an `agentic-analytics` subcommand; see Tech Spec §4.
.PHONY: load gold smoke run report status test gates

load:
	uv run agentic-analytics load --engine all

gold:
	uv run agentic-analytics gold

smoke:
	uv run agentic-analytics smoke

run:
	uv run agentic-analytics run --backend all --fix none --trials 3

report:
	uv run agentic-analytics report --run latest

status:
	uv run python scripts/status.py

test:
	uv run pytest -q

gates:
	uv run python scripts/node_converge.py $(NODE)
