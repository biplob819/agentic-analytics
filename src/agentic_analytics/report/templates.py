"""Page skeleton, CSS and the one inline script of the dashboard (B3.1, EvalKit D-42).

Stdlib `string.Template` only. The CSS defines light and dark colour schemes through custom
properties, and every colour cue it adds (heat bands, verdicts, pass/fail) duplicates a symbol
or word already in the markup. The script only filters the failure explorer and opens every
`<details>` before printing; the page reads fine with scripts off.
"""

from __future__ import annotations

from string import Template

CSS = """
:root{--bg:#fbfaf7;--fg:#1d1d1b;--muted:#5f5e58;--card:#ffffff;--line:#dedad0;
--accent:#2f5d50;--warn-bg:#fff4d6;--warn-fg:#5c4400;--pass:#1f6b3a;--fail:#a1261b;
--syn-bg:#ece6ff;--syn-fg:#3b2a7a;--s0:#2f5d50;--s1:#8a4b0f;--s2:#3d4f9a;--s3:#7a2d6b;
--s4:#5a5a12;--grid:#ebe7de;--focus:#1a56db;--h0:#f6e3df;--h1:#f7ece0;--h2:#f4f1e6;
--h3:#e5efe6;--h4:#d3e8d9;--bar:#3d4f9a}
@media (prefers-color-scheme:dark){:root{--bg:#151513;--fg:#ecebe6;--muted:#aeada6;
--card:#1e1e1b;--line:#3a3934;--accent:#8cc4b0;--warn-bg:#3b3112;--warn-fg:#f5dc9a;
--pass:#7fd39a;--fail:#ff9d92;--syn-bg:#2b2350;--syn-fg:#d9ceff;--s0:#8cc4b0;--s1:#f0a868;
--s2:#9fb0f5;--s3:#e09ad2;--s4:#d6d47a;--grid:#2c2b27;--focus:#8ab4ff;--h0:#46241f;
--h1:#45321f;--h2:#36352a;--h3:#23392b;--h4:#1d4430;--bar:#9fb0f5}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 system-ui,-apple-system,
"Segoe UI",sans-serif}
main{max-width:1100px;margin:0 auto;padding:16px 16px 64px}
h1{font-size:1.6rem;margin:.2em 0}h2{font-size:1.25rem;margin:1.8em 0 .5em}
h3{font-size:1.05rem;margin:1em 0 .4em}
a{color:var(--accent)}
.skip{position:absolute;left:-9999px;top:0;background:var(--card);padding:8px 12px}
.skip:focus{left:8px;top:8px;z-index:10}
nav ul{list-style:none;display:flex;flex-wrap:wrap;gap:4px 16px;padding:0;margin:8px 0}
.synthetic{background:var(--syn-bg);color:var(--syn-fg);padding:10px 14px;border-radius:8px;
font-weight:600;border:2px dashed currentColor}
.pending{background:var(--warn-bg);color:var(--warn-fg);padding:10px 14px;border-radius:8px}
.stamp{margin:6px 0}.stamp.warn{border-left:4px solid var(--warn-fg);padding:4px 12px;
background:var(--warn-bg);color:var(--warn-fg);font-weight:600}
.meta{color:var(--muted);padding-left:18px;margin:6px 0}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;
margin:12px 0;overflow-x:auto}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-weight:600}caption{text-align:left;color:var(--muted);padding-bottom:6px}
.ci,.n,.strict,.muted{color:var(--muted);font-size:.9em}
td.heat strong{font-size:1.05em}
.h0{background:var(--h0)}.h1{background:var(--h1)}.h2{background:var(--h2)}
.h3{background:var(--h3)}.h4{background:var(--h4)}
.pass{color:var(--pass);font-weight:600}.fail{color:var(--fail);font-weight:600}
.chart{width:100%;height:auto;max-width:780px;display:block}
.chart .grid,.chart .sep{stroke:var(--grid)}
.chart .tick,.chart .axis,.chart .rowlabel,.chart .verdict{fill:var(--fg);font-size:12px}
.chart .value{fill:var(--fg);font-size:11px}
.chart .axis{font-weight:600}.chart .verdict{font-weight:600}
.chart .zero{stroke:var(--fg);stroke-dasharray:4 3}
.chart .whisker{stroke:var(--fg);stroke-width:1.4}
.chart .s0{fill:var(--s0)}.chart .s1{fill:var(--s1)}.chart .s2{fill:var(--s2)}
.chart .s3{fill:var(--s3)}.chart .s4{fill:var(--s4)}
.chart .up{fill:var(--pass)}.chart .down{fill:var(--fail)}
.chart .hollow circle,.chart .hollow rect,.chart .hollow polygon{fill:var(--card);
stroke:var(--fg);stroke-width:1.6}
.chart .verdict.up{fill:var(--pass)}.chart .verdict.down{fill:var(--fail)}
.chart .bar rect{fill:var(--bar)}
details{border:1px solid var(--line);border-radius:8px;margin:8px 0;background:var(--card)}
summary{cursor:pointer;padding:8px 12px}details>div{padding:0 12px 12px}
pre{background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:8px;
overflow-x:auto;white-space:pre-wrap;word-break:break-word;font-size:13px}
.legend-key{margin:6px 0 0}
.answer{white-space:pre-wrap}.caveat{display:block;color:var(--muted);font-size:.85em}
:focus-visible{outline:3px solid var(--focus);outline-offset:2px}
label{margin-right:12px}select{font:inherit;padding:2px 6px}
footer{margin-top:40px;border-top:1px solid var(--line);padding-top:12px;color:var(--muted)}
@media print{select,label,nav,.skip{display:none}details{break-inside:avoid}}
"""

JS = """
(function(){
  var b=document.getElementById('f-backend'), c=document.getElementById('f-config'),
      n=document.getElementById('f-count');
  function apply(){
    var items=document.querySelectorAll('#failure-list details'), shown=0;
    for(var i=0;i<items.length;i++){
      var d=items[i], ok=(!b.value||d.dataset.backend===b.value)&&
        (!c.value||d.dataset.config===c.value);
      d.hidden=!ok; if(ok){shown++;}
    }
    if(n){n.textContent='Showing '+shown+' of '+items.length+'.';}
  }
  if(b&&c){b.addEventListener('change',apply);c.addEventListener('change',apply);}
  window.addEventListener('beforeprint',function(){
    var ds=document.querySelectorAll('details');for(var i=0;i<ds.length;i++){ds[i].open=true}});
})();
"""

PAGE = Template(
    '<!doctype html><html lang="en"><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width, initial-scale=1">'
    '<meta name="color-scheme" content="light dark">'
    "<title>$title</title><style>$css</style></head><body>"
    '<a class="skip" href="#main">Skip to results</a>'
    '<main id="main" tabindex="-1">$header$nav$sections$footer</main>'
    "<script>$js</script></body></html>\n"
)

NAV = (
    ("overview", "Overview"),
    ("categories", "By category"),
    ("uplift", "Fix uplift"),
    ("cost", "Cost and clarification"),
    ("probes", "Safety probes"),
    ("failures", "Failure explorer"),
)


TREND_LABEL = "Run history and drift"  # V5.1 MON-07: EvalKit's trend page beside the dashboard


def nav(trend_href: str | None = None) -> str:
    items = "".join(f'<li><a href="#{sid}">{label}</a></li>' for sid, label in NAV)
    if trend_href is not None:
        items += f'<li><a href="{trend_href}">{TREND_LABEL}</a></li>'
    return f'<nav aria-label="Sections"><ul>{items}</ul></nav>'


def section(sid: str, heading: str, body: str) -> str:
    return (
        f'<section id="{sid}" aria-labelledby="{sid}-h"><h2 id="{sid}-h">{heading}</h2>'
        f"{body}</section>"
    )


def page(
    *, title: str, header: str, sections: str, footer: str, trend_href: str | None = None
) -> str:
    return PAGE.substitute(
        title=title,
        css=CSS,
        header=header,
        nav=nav(trend_href),
        sections=sections,
        footer=footer,
        js=JS,
    )
