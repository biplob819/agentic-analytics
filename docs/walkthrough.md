# Walkthrough GIF: the screens it shows

`docs/assets/walkthrough.gif` is the project's portfolio walkthrough. It is silent, loops, and
holds each screen for 2.5 seconds (25 seconds in all, 960 px wide). Every screen is a real capture
of committed outputs, with no keys and no spend. The run of all fixes together appears on no
screen: it stays in the dashboard and is never a headline figure (D-105).

| # | Screen | Source |
|---|---|---|
| 1 | README headline: accuracy of every configuration, strict score as a ring | `docs/assets/headline.svg` |
| 2 | Dashboard: the run's stamp, licence credit and overview table | `docs/results/summary.json`, rendered by `src/agentic_analytics/report/render.py` |
| 3 | Fix uplift for each single fix, with the Holm-corrected verdict | `docs/assets/uplift.svg` |
| 4 | Dashboard: baseline accuracy by question category | same dashboard, "Accuracy by category", cropped below the table |
| 5 | Dashboard: safety probes | same dashboard, "Safety probes" |
| 6 | CLI: init, pack check, report --example and a dry run | `docs/assets/cli.png` |
| 7 | Demo: saved answers side by side, first sample question | `demo/app.py` with `demo/replay.json`, sample-question mode |
| 8 | Demo: a second sample question (top 10 towns), rows and SQL | same, sample switched |
| 9 | Demo: ask your own (passcode), live compute off, nothing entered | same, "Ask your own (passcode)" mode |
| 10 | Case study: the opening | `docs/case-study.md` |

Captured at 1280×800 with headless Google Chrome through Playwright. The dashboard was rendered
from the committed published-run record (`docs/results/`) into a scratch folder with no run
records, so its failure explorer is empty and not shown. The SVG figures, the CLI screenshot and
the case study were opened as local pages. The demo ran locally in an empty environment (no
`.env`, no secrets, saved answers only); the sample switch and the passcode mode were reached by
clicking, and no passcode, key or question was typed. The screens were joined with ffmpeg at a
128-colour palette.
