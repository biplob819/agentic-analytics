"""Streamlit Community Cloud entry for the Agentic Analytics demo (D-57, D4.2).

The hosted app runs from a checkout of this repo, not an install: this file puts `src/` on the
path and calls the page's `main` (all layout and logic live in `agentic_analytics.demo.page`,
tested without Streamlit). Packages come from the pinned `demo/requirements.txt`; secrets from
the app's secret store (`uv run python scripts/streamlit_secrets.py | pbcopy` builds them from
`.env`). Answers are saved answers unless the owner sets `DEMO_LIVE_BACKENDS` (OWNER D-59).

Run locally:  uv run --extra demo streamlit run demo/app.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "src") not in sys.path:  # the hosted app runs from a checkout, not an install
    sys.path.insert(0, str(ROOT / "src"))

from agentic_analytics.demo.page import main  # noqa: E402

main()
