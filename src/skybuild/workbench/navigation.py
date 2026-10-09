"""Shared navigation for the task workbench and its status views."""
from __future__ import annotations

from html import escape

from .source import page


_HOME_ICON = '<path d="m3 11 9-8 9 8"/><path d="M5.5 10v10h13V10M9 20v-6h6v6"/>'


def sidebar(active: str) -> str:
    """Render the single navigation rail with each source view's sections."""
    home_current = ' aria-current="page"' if active == "workbench" else ""
    parts = [
        '<nav class="workbench-shell-nav" aria-label="SkyBuild sections">',
        f'<a class="workbench-home" href="/workbench" title="Home workbench"{home_current}>'
        f'<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">{_HOME_ICON}</svg>'
        '<span class="workbench-nav-label">Home workbench</span></a>',
        '<ul class="workbench-nav-home-subsections">'
        '<li><a href="/workbench#tasks-title">Tasks</a></li>'
        '<li><a href="/workbench#create-title">Create a task</a></li>'
        '<li><a href="/workbench#detail-title">Selected task</a></li>'
        '<li><a href="/workbench#lineage-title">Structural lineage</a></li>'
        '<li><a href="/workbench#history-title">History</a></li>'
        '</ul>',
    ]
    for item in page.PAGES:
        key = item["key"]
        current = ' aria-current="page"' if active == key else ""
        parts.append(f'<section class="workbench-nav-group"><a class="workbench-view-link" href="/workbench/views/{key}"{current}>{escape(item["title"])}</a>')
        sections = item["sections"]
        if sections is None:
            sections = tuple(key for key in page.SECTION_TITLES if key not in page.HIDDEN_SECTIONS)
        if sections:
            parts.append('<ul class="workbench-nav-subsections">')
            for section in sections:
                title = page.SECTION_TITLES.get(section, section.replace("_", " ").title())
                parts.append(f'<li><a href="/workbench/views/{key}#section-{escape(section, quote=True)}">{escape(title)}</a></li>')
            parts.append('</ul>')
        parts.append('</section>')
    parts.append('</nav>')
    return "\n".join(parts)
