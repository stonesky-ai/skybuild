"""The page: its views, section titles, the static files inlined into one HTML document, and the CSP that hashes them.
"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent / "static"


def _static(name: str) -> str:
    """One of the page's own files under static/, read once at import: the server inlines it and the CSP hashes it."""
    return (STATIC / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------- the page
#
# One static page; the server writes no state into it. Its script fetches
# /api/state and puts every string on the page as text (textContent,
# createTextNode), never as markup, so no row sentence, question, reason or
# path an agent wrote can become markup. sessionview's panels arrive escaped
# with its badge spans added: the script takes those spans apart itself and
# rebuilds them as elements, and never hands the string to an HTML parser.
# Each refresh replaces only the boxes whose section changed.
#
# The page has views (the rail on its left): each shows some of the sections,
# the Everything view shows all of them. /view/<name> serves the same
# document; the script reads the name from the path, and an unknown name
# shows the default view. The health strip sits on every view.
#
# Three boxes carry buttons, and their only requests are this server's POST
# routes, sent as same-origin JSON (what `write_refusal` demands). Disk
# cleanup previews a plan — every directory it would delete — then runs only
# the items ticked, after a confirmation naming how many. Summaries asks for
# one topic's summary; the answer arrives with a later refresh, as text.
# Alarms acks one alarm, which the server sends on to the todo service.

#: Sections the page draws with their own controls rather than as data.
DRAWN_SECTIONS: tuple[str, ...] = ("summaries", "cleanup", "critical_path", "integrator_run",
                                   "alarms", "health", "queue", "integration", "seamstatus", "help", "usage", "landing",
                                   "flow", "barriers")
#: Sections that get no box of their own: the queue's lines are drawn inside the Queue box.
HIDDEN_SECTIONS: tuple[str, ...] = ("queue_lines",)

#: Section key -> box title, in page order. A section the server sends that is
#: not listed still gets a box, titled from its key, after these.
SECTION_TITLES: dict[str, str] = {
    "flow": "Build line",
    "overseer": "Overseer",
    "alarms": "Alarms",
    "health": "Health flags (cause and fix)",
    "fleet": "Fleet (agents and keepers per box, and why each keeper is idle)",
    "fleet_seams": "Seams by box (last 5 each)",
    "fleet_procs": "Fleet processes (command lines, sub-processes)",
    "barriers": "Barriers",
    "queue": "Queue (todo service)",
    "integration": "Integration board (todo service)",
    "seamstatus": "Seam status (todo service)",
    "summaries": "Summaries",
    "userquestions": "UserQuestions",
    "work": "Work",
    "seams": "Seams",
    "integrator_run": "Integrator now",
    "run_warnings": "Warnings (kept)",
    "batch_history": "Previous Batch Stats",
    "integrator": "Integrator",
    "in_process": "In-process",
    "next_up": "Next-up",
    "subagents": "Sub-agents",
    "who_where": "Who-Where",
    "unresolved": "Unresolved",
    "memory": "Memory",
    "disk": "Disk",
    "cleanup": "Disk cleanup",
    "loops": "Loops",
    "throughput": "Throughput",
    "usage": "Claude usage",
    "landing": "Landed per day (todo service)",
    "critical_path": "Critical path",
    "sessionview": "Workbench panels",
    "help": "Settings and help",
}

#: The views, in rail order: key, title, one line of what it answers, and the sections it shows in
#: the order it shows them, each with its width in twelfths of a wide screen (`sections` None: every
#: section, each half the screen). From 1000px to 1700px a section narrower than seven twelfths takes
#: half and the rest take all of it; in a window under 1000px every section takes all of it
#: (static/page.css). The page is laid out for a desktop screen and has no phone layout.
#: The first view is the default one: the build line across the top, then what needs a person.
_VIEWS: tuple[tuple[str, str, str, tuple[tuple[str, int], ...] | None], ...] = (
    ("alarms", "Home", "Is the build moving, and what needs a person now?",
     (("flow", 12), ("health", 5), ("alarms", 3), ("barriers", 4),
      ("userquestions", 5), ("usage", 3), ("landing", 4))),
    ("queue", "Queue", "What is open, claimed and blocked?",
     (("queue", 12), ("work", 6), ("next_up", 6), ("in_process", 12))),
    ("integration", "Integration", "What is ready to merge, and what is the gate doing?",
     (("integration", 12), ("seamstatus", 12), ("integrator_run", 7), ("integrator", 5), ("seams", 6), ("batch_history", 6),
      ("critical_path", 12))),
    ("boxes", "Boxes", "What does each box run, and what holds it back?",
     (("fleet", 12), ("fleet_seams", 6), ("memory", 6), ("loops", 6), ("fleet_procs", 12))),
    ("rows", "Streams and rows", "Who works on what, and what waits on the owner?",
     (("subagents", 6), ("who_where", 6), ("userquestions", 6), ("unresolved", 6), ("overseer", 6),
      ("sessionview", 12))),
    ("lanes", "Lanes and gates", "What are the lanes doing, and is the disk ready for them?",
     (("integrator_run", 8), ("throughput", 4), ("disk", 4), ("cleanup", 4), ("run_warnings", 12),
      ("summaries", 12))),
    ("usage", "Usage", "How much of the week is left, and what did it buy?",
     (("usage", 4), ("landing", 4), ("throughput", 4), ("barriers", 12))),
    ("help", "Settings and help", "What is configured, what are the keys, what is not measured yet?",
     (("help", 12),)),
    ("all", "Everything", "Every section on one page.", None),
)
PAGES: tuple[dict, ...] = tuple(
    {"key": key, "title": title, "asks": asks,
     "sections": None if laid is None else tuple(name for name, _ in laid),
     "spans": {} if laid is None else dict(laid)}
    for key, title, asks, laid in _VIEWS)
DEFAULT_PAGE = "all"
#: A view name in /view/<name>: lower-case letters only, so a path never carries anything else.
PAGE_NAME = re.compile(r"[a-z]{1,20}")

#: The rail's icons: plain line drawings, one per view, drawn inline (nothing is fetched).
_ICONS: dict[str, str] = {
    "alarms": '<path d="M2.5 9 9 3l6.5 6"/><path d="M4.5 8v6.5h9V8"/><path d="M7.5 14.5v-4h3v4"/>',
    "queue": '<path d="M3 4.5h12M3 9h12M3 13.5h8"/>',
    "integration": '<circle cx="4.5" cy="4" r="1.6"/><circle cx="4.5" cy="14" r="1.6"/><circle cx="13.5" cy="7" r="1.6"/>'
                   '<path d="M4.5 5.6v6.8M13.5 8.6c0 3-3 3.4-6.5 3.4"/>',
    "boxes": '<rect x="3" y="3" width="12" height="4.5" rx="1"/><rect x="3" y="10.5" width="12" height="4.5" rx="1"/>'
             '<path d="M6 5.25h.01M6 12.75h.01"/>',
    "rows": '<path d="M4 3v12M9 3v12M14 3v12"/><path d="M4 7h5M9 11h5"/>',
    "lanes": '<path d="M3 15 6 3M15 15 12 3"/><path d="M9 3v2.5M9 8v2.5M9 13v2"/>',
    "usage": '<path d="M3 13a6 6 0 0 1 12 0"/><path d="M9 13 12 7.5"/><path d="M2.5 13h13"/>',
    "help": '<circle cx="9" cy="9" r="6.5"/><path d="M7 7.3a2 2 0 1 1 2.8 1.8c-.6.3-.8.7-.8 1.4"/><path d="M9 13h.01"/>',
    "all": '<rect x="3" y="3" width="5" height="5" rx="1"/><rect x="10" y="3" width="5" height="5" rx="1"/>'
           '<rect x="3" y="10" width="5" height="5" rx="1"/><rect x="10" y="10" width="5" height="5" rx="1"/>',
}


def page_list() -> list[dict]:
    """The views as the state names them: what the script's rail and jump list use."""
    return [{"key": p["key"], "title": p["title"], "asks": p["asks"],
             "sections": list(p["sections"]) if p["sections"] is not None else None,
             "spans": dict(p["spans"])} for p in PAGES]


def _rail() -> str:
    links = []
    for p in PAGES:
        links.append(
            f'<a class="rail-link" href="/workbench/views/{p["key"]}" data-page="{p["key"]}" title="{html.escape(p["asks"])}">'
            f'<svg viewBox="0 0 18 18" width="18" height="18" aria-hidden="true" fill="none" stroke="currentColor" '
            f'stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">{_ICONS[p["key"]]}</svg>'
            f'<span>{html.escape(p["title"])}</span></a>')
    return "\n".join(links)


#: Sections whose values are sessionview's escaped, badged panel text (`panel_html` in panels.py).
PANEL_SECTIONS: tuple[str, ...] = ("sessionview",)

PAGE_STYLE = _static("page.css")

#: The script is the static/*.js files in name order (10_core, 20_drawn, 30_pages, 40_boot): one script element.
SCRIPT_FILES: tuple[str, ...] = tuple(sorted(p.name for p in STATIC.glob("*.js")))
PAGE_SCRIPT_TEMPLATE = "\n".join(_static(name) for name in SCRIPT_FILES)


def _js_value(value: object) -> str:
    """A constant as a JavaScript literal that cannot close the script element."""
    return json.dumps(value).replace("<", "\\u003c")


def _csp_hash(source: str) -> str:
    return "'sha256-" + base64.b64encode(hashlib.sha256(source.encode("utf-8")).digest()).decode("ascii") + "'"


PAGE_SCRIPT = (PAGE_SCRIPT_TEMPLATE
               .replace("__TITLES__", _js_value(list(SECTION_TITLES.items())))
               .replace("__PANELS__", _js_value(list(PANEL_SECTIONS)))
               .replace("__DRAWN__", _js_value(list(DRAWN_SECTIONS)))
               .replace("__HIDDEN__", _js_value(list(HIDDEN_SECTIONS)))
               .replace("__PAGES__", _js_value(page_list()))
               .replace("__DEFAULT_PAGE__", _js_value(DEFAULT_PAGE)))

PAGE_HTML = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Build status</title>
<style>{PAGE_STYLE}</style>
</head>
<body>
<link rel="stylesheet" href="/workbench/assets/workbench-shell.css">
<!--WORKBENCH_NAV-->
<div id="todo-warnings"></div>
<div class="shell">
<nav id="rail" aria-label="Views">
{_rail()}
</nav>
<div class="view">
<header>
<div id="health" class="health unknown" role="status"><span class="dot" aria-hidden="true"></span><span id="health-text">health: not read yet</span></div>
<div class="bar">
<div class="headline">
<h1 id="view-title">Build status</h1>
<p id="asks"></p>
</div>
<div class="tools">
<p id="meta">loading</p>
<p class="refresh"><span id="countdown"></span> <button type="button" id="pause" class="small">pause</button>
<button type="button" id="density" class="small">comfortable</button>
<button type="button" id="theme" class="small">theme: system</button>
<button type="button" id="print" class="small">print view</button>
<span id="snapshot-slot" class="topbtns"></span>
<a href="/workbench/queue.html">Todo queue</a></p>
<p id="status"></p>
</div>
</div>
</header>
<div id="alarm"></div>
<p id="changed" class="changed"></p>
<p id="loading">Reading the build state.</p>
<main id="boxes"></main>
</div>
</div>
<div id="palette" class="palette" hidden>
<input id="palette-input" type="text" placeholder="Jump to a view: type, Enter" autocomplete="off">
<ul id="palette-list"></ul>
</div>
<script>{PAGE_SCRIPT}</script>
</body>
</html>
"""

#: Only the page's own script and style run, and it talks to this server only.
PAGE_CSP = (f"default-src 'none'; script-src {_csp_hash(PAGE_SCRIPT)}; style-src {_csp_hash(PAGE_STYLE)}; "
            "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
