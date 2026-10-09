"""Shared navigation for the task workbench and its status views."""
from __future__ import annotations

from html import escape

from .source import page


_HOME_ICON = '<path d="m3 11 9-8 9 8"/><path d="M5.5 10v10h13V10M9 20v-6h6v6"/>'
_TASKS_ICON = '<path d="M5 4h13v16H5z"/><path d="M8 8h7M8 12h7M8 16h4"/><path d="m3 6 1 1 2-2"/>'
_MILESTONES_ICON = '<circle cx="12" cy="5" r="2"/><circle cx="12" cy="19" r="2"/><path d="M12 7v10M7 12h10"/><path d="m7 12-3-3m3 3-3 3m13-3 3-3m-3 3 3 3"/>'


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
    tasks_current = ' aria-current="page"' if active == "tasks" else ""
    parts.append(
        f'<a class="workbench-top-link" href="/workbench/tasks" title="Tasks"{tasks_current}>'
        f'<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">{_TASKS_ICON}</svg>'
        '<span>Tasks</span></a>'
    )
    milestones_current = ' aria-current="page"' if active == "milestones" else ""
    parts.append(
        f'<a class="workbench-top-link" href="/workbench/milestones" title="Milestones"{milestones_current}>'
        f'<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">{_MILESTONES_ICON}</svg>'
        '<span>Milestones</span></a>'
    )
    for item in page.PAGES:
        key = item["key"]
        current = ' aria-current="page"' if active == key else ""
        nav_title = "Fleet" if key == "boxes" else item["title"]
        parts.append(f'<section class="workbench-nav-group"><a class="workbench-view-link" href="/workbench/views/{key}"{current}>{escape(nav_title)}</a>')
        sections = item["sections"]
        if sections is None:
            sections = tuple(key for key in page.SECTION_TITLES if key not in page.HIDDEN_SECTIONS)
        if sections:
            parts.append('<ul class="workbench-nav-subsections">')
            for section in sections:
                title = "Members" if key == "boxes" and section == "fleet" else page.SECTION_TITLES.get(section, section.replace("_", " ").title())
                parts.append(f'<li><a href="/workbench/views/{key}#section-{escape(section, quote=True)}">{escape(title)}</a></li>')
            parts.append('</ul>')
        parts.append('</section>')
    parts.append('</nav>')
    return "\n".join(parts)
