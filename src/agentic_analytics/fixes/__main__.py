"""`python -m agentic_analytics.fixes [--pack P] [--force] [--check]`: freeze or check (FIX-05)."""

import sys

from agentic_analytics.fixes.freeze import main

sys.exit(main())
