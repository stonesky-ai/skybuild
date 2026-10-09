"""The terminal markdown rendering of the same state (`sessionview-console`).
"""
from __future__ import annotations

import html
import re
from collections.abc import Mapping
from typing import Any

from . import hub as sv


def _markdown_text(value: Any, *, panel: bool = False) -> str:
    """One untrusted value as markdown text, never executable HTML."""
    raw = str(value)
    if panel:
        # The legacy collector returns escaped text plus its own badge spans.
        # Strip only those generated spans before decoding the escaped text.
        raw = re.sub(r'</?span(?: class="badge badge-[a-z]+")?>', "", raw)
        raw = html.unescape(raw)
    return (raw.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace("|", "\\|").replace("\n", "  \n"))


def _markdown_value(value: Any, *, indent: int = 0, panel: bool = False) -> list[str]:
    """Expand every JSON field and row; empty values remain visible."""
    pad = "  " * indent
    if isinstance(value, Mapping):
        if not value:
            return [pad + "- none"]
        lines = []
        for key, item in value.items():
            label = _markdown_text(key)
            if isinstance(item, (Mapping, list)):
                lines.append(f"{pad}- **{label}**")
                lines.extend(_markdown_value(item, indent=indent + 1, panel=panel))
            else:
                lines.append(f"{pad}- **{label}:** {_markdown_text(item, panel=panel)}")
        return lines
    if isinstance(value, list):
        if not value:
            return [pad + "- none"]
        lines = []
        for item in value:
            if isinstance(item, (Mapping, list)):
                lines.append(pad + "- item")
                lines.extend(_markdown_value(item, indent=indent + 1, panel=panel))
            else:
                lines.append(f"{pad}- {_markdown_text(item, panel=panel)}")
        return lines
    return [pad + "- " + _markdown_text(value, panel=panel)]


def render_markdown(state: Mapping[str, Any]) -> str:
    """Terminal version of the page, from the same state and section order."""
    sections = state.get("sections")
    if not isinstance(sections, Mapping):
        raise TypeError("the page state has no sections")
    lines = ["# sessionview", ""]
    for key in ("repo", "generated_text"):
        if key in state:
            lines.append(f"- **{key}:** {_markdown_text(state[key])}")
    if len(lines) > 2:
        lines.append("")
    # The todo service's warnings come first, as on the page; none, no heading.
    todo = state.get("todo_warnings")
    if isinstance(todo, list) and todo:
        lines.extend(["## Todo service warnings", ""])
        for item in todo:
            if isinstance(item, Mapping):
                lines.append(f"- **{_markdown_text(item.get('headline', ''))}** "
                             f"{_markdown_text(item.get('fix', ''))}")
        lines.append("")
    # The health strip comes next, as on every page: one line, colour word first.
    health = sections.get("health")
    if isinstance(health, Mapping) and health.get("headline"):
        lines.append(f"**{_markdown_text(health.get('state', 'unknown')).upper()}** "
                     f"{_markdown_text(health['headline'])}")
        lines.append("")
    for key, title in (("alarm", "Alarm"), ("integrator_lease", "Integrator lease")):
        if state.get(key) is not None:
            lines.extend([f"## {title}", ""])
            lines.extend(_markdown_value(state[key]))
            lines.append("")
    ordered = [key for key in sv.SECTION_TITLES if key in sections]
    ordered.extend(key for key in sections if key not in sv.SECTION_TITLES)
    for key in ordered:
        title = sv.SECTION_TITLES.get(key, key.replace("_", " "))
        lines.extend([f"## {_markdown_text(title)}", ""])
        lines.extend(_markdown_value(sections[key], panel=key in sv.PANEL_SECTIONS))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def markdown_section_problems(state: Mapping[str, Any], markdown: str) -> list[str]:
    """Section parity check for tests and callers that export the page."""
    sections = state.get("sections")
    if not isinstance(sections, Mapping):
        return ["sections"]
    headings = {line for line in markdown.splitlines() if line.startswith("## ")}
    return [key for key in sections
            if f"## {_markdown_text(sv.SECTION_TITLES.get(key, key.replace('_', ' ')))}" not in headings]
