
"use strict";
const TITLES = __TITLES__;
const PANELS = new Set(__PANELS__);
// Sections drawn with their own buttons (drawn below) rather than as data.
const DRAWN = new Set(__DRAWN__);
// Sections the state carries for the server's own readers and the page draws inside another box.
const HIDDEN = new Set(__HIDDEN__);
// The views (the rail): each shows some sections; `sections: null` shows every one.
const PAGES = __PAGES__;
const DEFAULT_PAGE = __DEFAULT_PAGE__;
// The badge states the style sheet knows: sessionview's, then the barriers'.
const BADGES = new Set(["running", "ready", "idle", "quiet", "blocked", "stuck", "stopped",
                        "bad", "warn", "unknown", "ok", "info"]);
const titles = new Map(TITLES);
const order = new Map(TITLES.map((pair, i) => [pair[0], i]));
const boxes = new Map();
const grid = document.getElementById("boxes");
const meta = document.getElementById("meta");
const status = document.getElementById("status");
const alarm = document.getElementById("alarm");
const todoWarnings = document.getElementById("todo-warnings");
// The chrome around the boxes; a saved snapshot or a bare harness may lack any of these, so each is checked.
const healthBar = document.getElementById("health");
const healthText = document.getElementById("health-text");
const viewTitle = document.getElementById("view-title");
const countdown = document.getElementById("countdown");
const changedLine = document.getElementById("changed");
const rail = document.getElementById("rail");
let todoShown = null;
const own = (obj, key) => Object.prototype.hasOwnProperty.call(obj, key);
let interval = 15000;

// Density: compact (the default: one line per row, 1-2px of padding) or comfortable, kept in this browser.
const DENSITY_KEY = "workbench.density";
let comfortable = false;
try { comfortable = window.localStorage.getItem(DENSITY_KEY) === "comfortable"; } catch (err) {}

function applyDensity() {
  if (!document.body || !document.body.classList) return;
  document.body.classList.toggle("comfortable", comfortable);
}

// A time the todo service wrote (ISO, UTC): relative in the cell, the absolute time in the tooltip.
function relative(ms, now) {
  const s = Math.round((now - ms) / 1000);
  const a = Math.abs(s);
  const unit = a < 60 ? a + " s" : a < 3600 ? Math.round(a / 60) + " min"
    : a < 86400 ? (Math.round(a / 360) / 10) + " h" : Math.round(a / 86400) + " d";
  return s >= 0 ? unit + " ago" : "in " + unit;
}

function whenNode(iso, now) {
  const node = el("span", "when");
  const text = scalarText(iso);
  const ms = Date.parse(text);
  if (!Number.isFinite(ms)) { node.textContent = text; return node; }
  node.textContent = relative(ms, now === undefined ? Date.now() : now);
  node.title = text;
  return node;
}

// The theme: the system's until the viewer picks light or dark, and that choice is kept in this browser.
const THEME_KEY = "workbench.theme";
const THEMES = ["system", "light", "dark"];
let theme = "system";
try {
  const kept = window.localStorage.getItem(THEME_KEY);
  if (THEMES.includes(kept)) theme = kept;
} catch (err) {}

function applyTheme() {
  const root = document.documentElement;
  if (!root || typeof root.setAttribute !== "function") return;
  if (theme === "system") root.removeAttribute("data-theme"); else root.setAttribute("data-theme", theme);
}
applyTheme();

// How much of a long table shows before the viewer asks for the rest. A table only a few rows longer
// than that is shown whole: a button that uncovers three rows costs more than it saves.
const ROWS_SHOWN = 12;
const ROWS_SLACK = 3;
// How many groups of a grouped table, and how many lists of a box, start open; the rest start shut.
const GROUPS_OPEN = 3;
const LISTS_OPEN = 2;
// Tables the viewer opened in full, by where they sit on the page; kept for the visit, so a refresh
// that repaints the box does not shut them again.
const opened = new Set();

// Where the renderer is now: the section, then each field or heading on the way down. It names a
// table or a fold, so the viewer's "show all" and plus/minus choices find it again after a repaint.
const renderPath = [];

function pathKey(leaf) {
  return renderPath.concat(leaf === undefined || leaf === "" ? [] : [String(leaf)]).join("/");
}

function within(leaf, build) {
  renderPath.push(String(leaf));
  try { return build(); } finally { renderPath.pop(); }
}

// Folds: a part of a box that opens (a minus) and shuts (a plus). What the viewer chose is kept in
// this browser by the fold's place on the page; a fold never touched opens or shuts as its caller says.
const FOLDS_KEY = "workbench.folds";
const FOLDS_KEPT = 400;
const folds = storedFolds();

function storedFolds() {
  const found = new Map();
  try {
    const raw = JSON.parse(window.localStorage.getItem(FOLDS_KEY) || "{}");
    if (isScalar(raw) || Array.isArray(raw)) return found;
    for (const [key, open] of Object.entries(raw)) if (typeof open === "boolean") found.set(key, open);
  } catch (err) {}
  return found;
}

function keepFolds() {
  while (folds.size > FOLDS_KEPT) folds.delete(folds.keys().next().value);
  try { window.localStorage.setItem(FOLDS_KEY, JSON.stringify(Object.fromEntries(folds))); } catch (err) {}
}

// A group of rows that is over, or has not begun, starts shut; what is happening now starts open.
const PAST = new Set(["done", "landed", "cleared", "finished", "passed", "merged", "dropped", "acked", "closed"]);
const FUTURE = new Set(["next", "pending", "deferred", "queued", "later"]);

function startsOpen(label) {
  const word = String(label).trim().toLowerCase();
  return !PAST.has(word) && !FUTURE.has(word);
}

function fold(name, title, content, open, aside) {
  const key = pathKey(name);
  const box = el("details", "fold");
  box.open = folds.has(key) ? folds.get(key) : open !== false;
  const head = el("summary");
  head.appendChild(el("span", "", title));
  if (aside) head.appendChild(el("span", "muted", aside));
  box.appendChild(head);
  box.appendChild(content);
  if (typeof box.addEventListener === "function") {
    box.addEventListener("toggle", () => {
      folds.set(key, box.open === true);
      keepFolds();
    });
  }
  return box;
}

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}

function titleOf(key) {
  if (titles.has(key)) return titles.get(key);
  const words = String(key).replace(/[_.\-]+/g, " ").trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

function human(key) {
  return String(key).replace(/_/g, " ");
}

function isScalar(value) {
  return value === null || value === undefined || typeof value !== "object";
}

function scalarText(value) {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "boolean") return value ? "yes" : "no";
  if (typeof value === "number" && !Number.isInteger(value)) return String(Math.round(value * 100) / 100);
  return String(value);
}

// A time as the todo service writes it (ISO, UTC): the data is the absolute time, the cell says how long ago.
const ISO_TIME = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/;

function scalar(value, key) {
  const text = scalarText(value);
  if (key === "state" && BADGES.has(text)) return el("span", "badge badge-" + text, text);
  if (typeof value === "string" && value.includes("\n")) return el("pre", "", text);
  if (typeof value === "string" && ISO_TIME.test(value)) return whenNode(value);
  return el("span", value === null || value === undefined || value === "" ? "muted" : "", text);
}

function empty(value) {
  return value === null || value === undefined || value === "";
}

// The first column whose values are so repeated that they read better as
// headings than as a column: every value a one-line scalar, and the distinct
// values few against the row count. The id-like columns (distinct per row)
// never qualify.
function groupColumn(rows, columns) {
  if (rows.length < 3 || columns.length < 3) return null;
  for (const key of columns) {
    const values = rows.map(row => own(row, key) ? row[key] : "");
    if (!values.every(value => isScalar(value) && !String(scalarText(value)).includes("\n"))) continue;
    const distinct = new Set(values.map(scalarText));
    if (distinct.size === 1 || distinct.size * 3 <= rows.length) return key;
  }
  return null;
}

const CLIP_AT = 48;
// A long text cell shows one line cut with an ellipsis (full text in the tooltip); a click opens it and a second click closes it.
// The cell is in the tab order, and Enter or Space does what a click does.
function clipCell(cell) {
  const text = cell.textContent || "";
  if (text.length <= CLIP_AT) return;
  cell.className = (cell.className ? cell.className + " " : "") + "clip";
  cell.title = text;
  cell.tabIndex = 0;
  const toggle = () => {
    cell.className = / open\b/.test(" " + cell.className)
      ? cell.className.replace(/\s*\bopen\b/, "") : cell.className + " open";
  };
  if (typeof cell.addEventListener === "function") {
    cell.addEventListener("click", toggle);
    cell.addEventListener("keydown", ev => {
      if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); toggle(); }
    });
  }
}

// The order two cells sort in: numbers (durations, counts, percentages) by value, the rest as text.
function sortValue(value) {
  const text = scalarText(value);
  if (/^\d{4}-\d\d-\d\d/.test(text)) return text.toLowerCase();
  const num = parseFloat(text.replace(/,/g, ""));
  return Number.isFinite(num) && /^-?[\d.,]/.test(text) ? num : text.toLowerCase();
}

// A table of `rows`. With a `key` (where it sits on the page) a long table shows its first rows
// (ROWS_SHOWN of them, or the `cap` its caller names) and a button for the rest; sorting sorts every
// row, then shows the first of them again.
function tableOf(rows, columns, key, cap) {
  const first = Number.isInteger(cap) && cap > 0 ? cap : ROWS_SHOWN;
  const wrap = el("div", "scroll");
  const sheet = el("table");
  const head = sheet.createTHead().insertRow();
  // A column whose every filled cell is a number or a duration/count reads right-aligned.
  const numeric = key => rows.some(row => own(row, key) && !empty(row[key])) &&
    rows.every(row => !own(row, key) || empty(row[key]) || /^-?[\d.,]+(?:[dhms%]|[\d.,:])*$/.test(scalarText(row[key])));
  const numbers = new Set(columns.filter(numeric));
  const body = sheet.createTBody();
  const long = Boolean(key) && rows.length > first + ROWS_SLACK;
  let current = rows;
  const fill = shown => {
    current = shown;
    body.replaceChildren();
    for (const row of (long && !opened.has(key) ? shown.slice(0, first) : shown)) {
      const line = body.insertRow();
      for (const key of columns) {
        const cell = line.insertCell();
        if (numbers.has(key)) cell.className = "num";
        if (own(row, key)) cell.appendChild(render(row[key], key));
        if (!numbers.has(key)) clipCell(cell);
      }
    }
  };
  // A heading sorts its column (a click, or Enter or Space on it); the same heading again sorts the other way.
  let sortedBy = null, ascending = true;
  for (const key of columns) {
    const th = el("th", numbers.has(key) ? "num" : "", human(key));
    th.tabIndex = 0;
    th.title = "Sort by " + human(key);
    const sort = () => {
      ascending = sortedBy === key ? !ascending : true;
      sortedBy = key;
      const ordered = rows.slice().sort((a, b) => {
        const x = sortValue(own(a, key) ? a[key] : ""), y = sortValue(own(b, key) ? b[key] : "");
        const cmp = typeof x === "number" && typeof y === "number" ? x - y : String(x).localeCompare(String(y));
        return ascending ? cmp : -cmp;
      });
      for (const other of head.children) other.className = other.className.replace(/\s*\bsorted-(asc|desc)\b/, "");
      th.className += ascending ? " sorted-asc" : " sorted-desc";
      fill(ordered);
    };
    if (typeof th.addEventListener === "function") {
      th.addEventListener("click", sort);
      th.addEventListener("keydown", ev => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); sort(); } });
    }
    head.appendChild(th);
  }
  fill(rows);
  wrap.appendChild(sheet);
  if (!long) return wrap;
  const outer = el("div");
  outer.appendChild(wrap);
  const more = el("div", "more");
  const count = el("span", "", "");
  const toggle = button("", "small");
  const label = () => {
    const all = opened.has(key);
    count.textContent = (all ? rows.length : first) + " of " + rows.length + " rows shown";
    toggle.textContent = all ? "Show the first " + first : "Show all " + rows.length;
  };
  toggle.addEventListener("click", () => {
    if (opened.has(key)) opened.delete(key); else opened.add(key);
    fill(current);
    label();
  });
  label();
  more.appendChild(count);
  more.appendChild(toggle);
  outer.appendChild(more);
  return outer;
}

// `name` tells this table from another under the same heading (a drawn section passes one).
function table(rows, name) {
  const key = pathKey(name);
  const columns = [];
  for (const row of rows) for (const key of Object.keys(row)) if (!columns.includes(key)) columns.push(key);
  // A column empty in every row says nothing: dropped.
  const filled = columns.filter(key => rows.some(row => own(row, key) && !empty(row[key])));
  const groupKey = groupColumn(rows, filled);
  if (groupKey === null) return tableOf(rows, filled.length ? filled : columns, key);
  const rest = filled.filter(key => key !== groupKey);
  const groups = new Map();
  for (const row of rows) {
    const label = scalarText(own(row, groupKey) ? row[groupKey] : "");
    if (!groups.has(label)) groups.set(label, []);
    groups.get(label).push(row);
  }
  const wrap = el("div");
  // Each group folds. One that is over or not begun starts shut; of the rest the first GROUPS_OPEN
  // start open, so a table of twenty groups is a list of headings with its head showing.
  let shown = 0;
  for (const [label, group] of groups) {
    const open = startsOpen(label) && shown < GROUPS_OPEN;
    if (open) shown += 1;
    wrap.appendChild(fold((name || "") + "#" + label, human(groupKey) + ": " + label,
      tableOf(group, rest, key + "#" + label), open, group.length === 1 ? "1 row" : group.length + " rows"));
  }
  return wrap;
}

function fields(obj) {
  const list = el("dl");
  let lists = 0;
  for (const [key, value] of Object.entries(obj)) {
    if (key === "error") {
      if (value) list.appendChild(el("div", "error", scalarText(value)));
      continue;
    }
    // A list of rows is a part of the box that folds, its count beside its name; the first LISTS_OPEN start open.
    if (Array.isArray(value) && value.length && value.every(v => !isScalar(v) && !Array.isArray(v))) {
      const open = startsOpen(key) && lists < LISTS_OPEN;
      lists += 1;
      list.appendChild(fold(key, human(key), within(key, () => render(value, key)), open,
        value.length === 1 ? "1 row" : value.length + " rows"));
      continue;
    }
    list.appendChild(el("dt", "", human(key)));
    const item = el("dd");
    item.appendChild(key === "charts" ? charts(value) : within(key, () => render(value, key)));
    list.appendChild(item);
  }
  return list;
}

// A chart arrives as data {tag, attrs, text, children}; its nodes are made here, never parsed from markup.
const SVG_NS = ["http:", "www.w3.org", "2000", "svg"].join("/").replace(":/", "://"); // the page names no outside URL literally
const SVG_TAGS = new Set(["svg", "title", "line", "polyline", "circle", "text"]);

const SVG_REFUSED = /^(on|style$|href$|xlink)/i;

function svgNode(spec) {
  const node = document.createElementNS(SVG_NS, SVG_TAGS.has(spec.tag) ? spec.tag : "g");
  // A plain attribute name only: letters, digits and hyphens. The capitals let viewBox and preserveAspectRatio
  // through, which a chart needs to scale, and the digits x1, y1, x2 and y2, without which a line is a dot at
  // the origin. Never an event handler, a style or a link, whatever the spec says.
  for (const [name, text] of Object.entries(spec.attrs || {})) {
    if (/^[a-zA-Z][a-zA-Z0-9-]*$/.test(name) && !SVG_REFUSED.test(name)) node.setAttribute(name, text);
  }
  if (spec.text) node.appendChild(document.createTextNode(spec.text));
  for (const child of spec.children || []) node.appendChild(svgNode(child));
  return node;
}

function charts(value) {
  const wrap = el("div", "charts");
  for (const [name, spec] of Object.entries(value)) {
    const figure = el("figure");
    if (spec) figure.appendChild(svgNode(spec));
    else figure.appendChild(el("span", "muted", "no measured batch"));
    figure.appendChild(el("figcaption", "", name));
    wrap.appendChild(figure);
  }
  return wrap;
}

function render(value, key) {
  if (isScalar(value)) return scalar(value, key);
  if (!Array.isArray(value)) return key === "charts" ? charts(value) : fields(value);
  if (value.length === 0) return el("span", "muted", "none");
  if (value.every(isScalar)) return el("span", "", value.map(scalarText).join(" · "));
  if (value.every(v => !isScalar(v) && !Array.isArray(v))) return table(value);
  const list = el("ul");
  for (const item of value) {
    const entry = el("li");
    entry.appendChild(render(item));
    list.appendChild(entry);
  }
  return list;
}
