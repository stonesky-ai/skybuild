// How wide a section is on a wide screen, in twelfths: what its view says (page.py, PAGES), else half.
const DEFAULT_SPAN = 6;

function spanOf(key) {
  const page = pageByKey.get(currentPage);
  const spans = page && page.spans && !isScalar(page.spans) ? page.spans : {};
  const span = own(spans, key) ? spans[key] : PANELS.has(key) ? 12 : DEFAULT_SPAN;
  return Number.isInteger(span) && span >= 3 && span <= 12 ? span : DEFAULT_SPAN;
}

// A box is its title and its body, as tall as what the body shows; the grid gives it its width.
function boxFor(key) {
  let entry = boxes.get(key);
  if (entry) return entry;
  const box = el("section", "box");
  box.appendChild(el("h2", "", titleOf(key)));
  const body = el("div", "body");
  box.appendChild(body);
  entry = {box: box, body: body, last: null};
  boxes.set(key, entry);
  return entry;
}

// The metered-endpoint banner, above every box. Text only, like the rest of
// the page: every string goes in as textContent, never as markup.
function paintAlarm(value, lease) {
  alarm.replaceChildren();
  if (lease && !isScalar(lease) && !Array.isArray(lease)) {
    const box = document.createElement("p");
    const state = ["fresh", "stale", "absent", "unreadable"].includes(lease.state)
      ? lease.state : "unreadable";
    box.className = "integrator-lease " + state;
    const head = document.createElement("strong");
    head.textContent = scalarText(lease.headline);
    const detail = document.createElement("span");
    detail.textContent = scalarText(lease.detail);
    box.append(head, detail);
    alarm.append(box);
  }
  document.title = baseTitle();
  if (isScalar(value) || Array.isArray(value) || !value) return;
  if (value.fire) {
    const box = document.createElement("p");
    box.className = "fire";
    const head = document.createElement("strong");
    head.className = "headline";
    head.textContent = scalarText(value.headline);
    const why = document.createElement("span");
    why.className = "why";
    why.textContent = scalarText(value.reason);
    box.append(head, why);
    for (const line of [value.howto, value.ack]) {
      if (!line) continue;
      const extra = document.createElement("span");
      extra.className = "why";
      extra.textContent = scalarText(line);
      box.append(extra);
    }
    alarm.append(box);
    document.title = scalarText(value.headline);
    return;
  }
  document.title = baseTitle();
  const armed = document.createElement("p");
  armed.className = "armed";
  const waiting = Array.isArray(value.outstanding) ? value.outstanding.map(scalarText) : [];
  armed.textContent = scalarText(value.name) + ": still needed \u2014 "
    + (waiting.length ? waiting.join("; ") : "no reason given");
  alarm.append(armed);
}

// The todo service's warnings, above everything: one red block per problem,
// its headline and what to change, as text. None: the slot is emptied, so no
// banner element is left on the page.
function paintTodoWarnings(list) {
  if (!todoWarnings) return;
  const items = Array.isArray(list) ? list.filter(w => w && !isScalar(w) && !Array.isArray(w)) : [];
  const json = JSON.stringify(items);
  if (json === todoShown) return;
  todoShown = json;
  todoWarnings.replaceChildren();
  for (const item of items) {
    const box = document.createElement("div");
    box.className = "todo-warning";
    const head = document.createElement("strong");
    head.textContent = scalarText(item.headline);
    const fix = document.createElement("span");
    fix.textContent = scalarText(item.fix);
    box.append(head, fix);
    todoWarnings.append(box);
  }
}

// The JSON of every section at the last paint, so a view switch can say what changed meanwhile.
const seen = new Map();

function paint(state) {
  lastState = state;
  paintTodoWarnings(state ? state.todo_warnings : null);
  const raw = state ? state.sections : null;
  const sections = isScalar(raw) || Array.isArray(raw) ? {} : raw;
  const changed = [];
  for (const [key, value] of Object.entries(sections)) {
    const json = JSON.stringify(value);
    if (seen.has(key) && seen.get(key) !== json) changed.push(key);
    seen.set(key, json);
  }
  paintHealth(sections.health);
  const loading = document.getElementById("loading");
  if (loading) loading.remove();
  const keys = visibleSections(Object.keys(sections)
    .filter(key => !HIDDEN.has(key))
    .map((key, i) => [order.has(key) ? order.get(key) : TITLES.length + i, key])
    .sort((a, b) => a[0] - b[0])
    .map(pair => pair[1]));
  const shown = new Set(keys);
  for (const [key, entry] of boxes) {
    if (!shown.has(key)) {
      entry.box.remove();
      boxes.delete(key);
    }
  }
  let previous = null;
  for (const key of keys) {
    const entry = boxFor(key);
    const cls = "box span-" + spanOf(key);
    if (entry.box.className !== cls) entry.box.className = cls;
    const json = JSON.stringify(sections[key]);
    // Only the box's body is replaced, and only when its section changed: the box itself stays.
    if (json !== entry.last) {
      entry.body.replaceChildren(within(key, () => PANELS.has(key) ? panels(sections[key])
        : DRAWN.has(key) ? drawn(key, sections[key]) : render(sections[key])));
      entry.last = json;
    }
    const expected = previous ? previous.nextSibling : grid.firstChild;
    if (expected !== entry.box) grid.insertBefore(entry.box, expected);
    previous = entry.box;
  }
  noteChanges(changed);
  paintAlarm(state.alarm, state.integrator_lease);
  meta.textContent = [state.repo, state.generated_text ? "as of " + state.generated_text : ""]
    .filter(Boolean).join(" · ");
}

// One read of the state, painted. Never throws: a failure is said on the page.
async function load() {
  try {
    const response = await fetch("/ideas/api/state", {cache: "no-store", credentials: "same-origin"});
    if (!response.ok) throw new Error("HTTP " + response.status);
    const state = await response.json();
    if (isScalar(state) || Array.isArray(state)) throw new Error("not a state document");
    const ms = Number(state.interval_ms);
    if (Number.isFinite(ms) && ms >= 1000) interval = ms;
    paint(state);
    // A server that could not rebuild the state says so, and what is shown is its last good one.
    status.className = state.build_error ? "error" : "";
    status.textContent = state.build_error
      ? "the server could not rebuild the state (" + scalarText(state.build_error) + "); the boxes show its last one" : "";
  } catch (err) {
    status.className = "error";
    status.textContent = "refresh failed (" + (err && err.message ? err.message : "no answer")
      + "); the boxes show the last answer";
  }
  nextAt = Date.now() + interval;
  tickCountdown();
}

// The one refresh chain; a button's own `load` repaints once and schedules nothing. Paused, it
// keeps ticking and fetches nothing, so resuming needs no reload.
async function refresh() {
  try {
    if (!paused) await load();
  } finally {
    setTimeout(refresh, interval);
  }
}

// Save snapshot: one press downloads this page as a single HTML file that shows what it shows now and talks to
// nothing. The page's own source is fetched again (not the live DOM), the current state is embedded in it, and
// the copy answers its own /api/state from that state. A saved copy has no button: it has no server to save from.
function snapshotStub(state, taken) {
  // Every less-than sign, not only an end tag's: a comment opener followed by a script start tag in a state
  // string would otherwise keep the stub's script element open and swallow the page's own script.
  const blob = JSON.stringify(state).replace(/</g, "\\u003c");
  return "<" + "script>\n// Snapshot taken " + taken + ": nothing here talks to a server.\n"
    + "window.__SNAPSHOT__ = " + blob + ";\n"
    + "window.__PAGE__ = " + JSON.stringify(currentPage) + ";\n"
    + "window.fetch = async function (url, opts) {\n"
    + "  const method = ((opts && opts.method) || 'GET').toUpperCase();\n"
    + "  if (method !== 'GET' || !String(url).includes('/ideas/api/state'))\n"
    + "    return new Response(JSON.stringify({refused: 'this is a saved snapshot: buttons do nothing'}), {status: 405});\n"
    + "  return new Response(JSON.stringify(window.__SNAPSHOT__), {status: 200, headers: {'Content-Type': 'application/json'}});\n"
    + "};\n"
    + "document.addEventListener('DOMContentLoaded', () => {\n"
    + "  const bar = document.createElement('div');\n"
    + "  bar.textContent = 'SNAPSHOT taken " + taken.replace(/['\\<>]/g, "") + " - not live. Nothing on this page updates or acts.';\n"
    + "  bar.style.cssText = 'position:sticky;top:0;z-index:99;padding:8px 14px;background:#f3c65b;color:#1a1300;font:600 13px system-ui,sans-serif';\n"
    + "  document.body.prepend(bar);\n"
    + "});\n"
    + "<" + "/script>\n";
}

async function saveSnapshot(button) {
  button.disabled = true;
  status.className = "";
  status.textContent = "building the snapshot...";
  try {
    const [pageResp, stateResp] = await Promise.all([
      fetch("/ideas", {cache: "no-store", credentials: "same-origin"}),
      fetch("/ideas/api/state", {cache: "no-store", credentials: "same-origin"})]);
    if (!pageResp.ok || !stateResp.ok) throw new Error("HTTP " + pageResp.status + " / " + stateResp.status);
    const html = await pageResp.text();
    const state = await stateResp.json();
    if (isScalar(state) || Array.isArray(state)) throw new Error("not a state document");
    state.interval_ms = 86400000;
    state.generated_text = scalarText(state.generated_text) + " (snapshot)";
    const now = new Date();
    const pad = n => String(n).padStart(2, "0");
    const stamp = now.getFullYear() + "-" + pad(now.getMonth() + 1) + "-" + pad(now.getDate())
      + "_" + pad(now.getHours()) + pad(now.getMinutes()) + pad(now.getSeconds());
    const at = html.indexOf("<" + "script");
    if (at < 0) throw new Error("the page source has no script to put the snapshot before");
    const out = html.slice(0, at) + snapshotStub(state, now.toString().slice(0, 24)) + html.slice(at);
    const file = new Blob([out], {type: "text/html"});
    const link = document.createElement("a");
    link.href = URL.createObjectURL(file);
    link.download = "workbench-" + stamp + ".html";
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(link.href), 60000);
    status.textContent = "snapshot saved as " + link.download + " (" + Math.round(out.length / 1024) + " KB)";
  } catch (err) {
    status.className = "error";
    status.textContent = "snapshot failed (" + (err && err.message ? err.message : "no answer") + ")";
  } finally {
    button.disabled = false;
  }
}

function installSnapshotButton() {
  if (typeof window.__SNAPSHOT__ !== "undefined") return;
  const slot = document.getElementById("snapshot-slot");
  if (!slot) return;
  const button = el("button", "snapbtn", "Save snapshot (HTML)");
  button.type = "button";
  button.title = "Download this page, as it is now, as one HTML file anyone can open";
  button.addEventListener("click", () => saveSnapshot(button));
  slot.appendChild(button);
}

paintRail();
installTools();
installSnapshotButton();
refresh();
