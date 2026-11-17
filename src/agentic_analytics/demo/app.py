"""Streamlit entry for the demo page (D4.2). Layout and logic live in `agentic_analytics.demo.page`.

The hosted app (Streamlit Community Cloud, D-57) runs the repo-root `demo/app.py`, which puts
`src/` on the path; this module is the entry for an installed package:

    pip install "agentic-analytics[demo]"
    streamlit run src/agentic_analytics/demo/app.py
"""

from agentic_analytics.demo.page import main

main()
