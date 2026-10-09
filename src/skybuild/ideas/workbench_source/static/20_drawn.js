// sessionview's panel text is escaped by sessionview, which then wraps its own
// state words in badge spans. Take the spans apart and unescape the rest as
// text; anything that is not such a span stays text, shown as it came.
const BADGE_SPAN = /<span class="badge badge-([a-z]+)">([^<]*)<\/span>/g;
const ENTITIES = {"&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": "\"", "&#x27;": "'"};
DRAWN.add("batch_history");

function unescapeText(text) {
  return text.replace(/&(?:amp|lt|gt|quot|#x27);/g, found => ENTITIES[found]);
}

function badged(markup) {
  const pre = el("pre");
  let at = 0;
  for (const found of markup.matchAll(BADGE_SPAN)) {
    pre.appendChild(document.createTextNode(unescapeText(markup.slice(at, found.index))));
    const state = BADGES.has(found[1]) ? found[1] : "";
    pre.appendChild(el("span", state ? "badge badge-" + state : "", unescapeText(found[2])));
    at = found.index + found[0].length;
  }
  pre.appendChild(document.createTextNode(unescapeText(markup.slice(at))));
  return pre;
}

function panels(value) {
  if (isScalar(value) || Array.isArray(value)) return render(value);
  // Each panel folds, the first one open: a panel is a screen of terminal text, and eight of them
  // open made the view five screens tall.
  const wrap = el("div");
  let first = true;
  for (const [name, markup] of Object.entries(value)) {
    const lines = typeof markup === "string" ? markup.split("\n").length : 0;
    wrap.appendChild(fold(name, name, typeof markup === "string" ? badged(markup) : within(name, () => render(markup)),
      first, lines ? lines + " lines" : ""));
    first = false;
  }
  return wrap;
}

// This server's own POST routes: a same-origin JSON body, the only shape
// `write_refusal` lets through. The answer is read as data whatever the status.
async function post(path, body) {
  const response = await fetch(path, {method: "POST", cache: "no-store", credentials: "same-origin",
                                      headers: {"Content-Type": "application/json"},
                                      body: JSON.stringify(body)});
  let answer = null;
  try { answer = await response.json(); } catch (err) {}
  if (isScalar(answer) || Array.isArray(answer)) answer = {};
  return {ok: response.ok, status: response.status, answer: answer};
}

function refusal(sent) {
  return "refused: " + scalarText(sent.answer.refused || sent.answer.error || ("HTTP " + sent.status));
}

function objects(value) {
  return Array.isArray(value) ? value.filter(v => !isScalar(v) && !Array.isArray(v)) : [];
}

function button(text, cls) {
  const node = el("button", cls || "", text);
  node.type = "button";
  return node;
}

function drawn(key, value) {
  if (key === "usage") return drawUsage(value);
  if (key === "landing") return drawLanding(value);
  if (isScalar(value) || Array.isArray(value)) return render(value);
  if (key === "flow") return drawFlow(value);
  if (key === "barriers") return drawBarriers(value);
  if (key === "summaries") return drawSummaries(value);
  if (key === "cleanup") return drawCleanup(value);
  if (key === "critical_path") return drawCriticalPath(value);
  if (key === "integrator_run") return drawIntegrator(value);
  if (key === "alarms") return drawAlarms(value);
  if (key === "health") return drawHealth(value);
  if (key === "queue") return drawQueue(value);
  if (key === "integration") return drawIntegration(value);
  if (key === "seamstatus") return drawSeamStatus(value);
  if (key === "batch_history") return drawBatchHistory(value);
  if (key === "help") return drawHelp(value);
  return render(value);
}

// Previous batches: one table, newest first, every batch the trunk records. It shows the newest
// `shown` of them and a button for the rest; it is never cut into groups, because a batch is one line
// of one history. The chart of every batch folds shut under the charts of the newest.
function drawBatchHistory(value) {
  const wrap = el("div");
  if (value.detail) wrap.appendChild(el("p", "muted note", scalarText(value.detail)));
  const rows = objects(value.rows);
  if (rows.length) {
    const columns = [];
    for (const row of rows) for (const key of Object.keys(row)) if (!columns.includes(key)) columns.push(key);
    wrap.appendChild(tableOf(rows, columns.filter(key => rows.some(row => !empty(row[key]))), pathKey("batches"), value.shown));
  }
  if (value.charts && !isScalar(value.charts)) wrap.appendChild(charts(value.charts));
  if (value.charts_all && !isScalar(value.charts_all))
    wrap.appendChild(fold("charts-all", "Every batch on one chart", charts(value.charts_all), false));
  return wrap;
}

// Seam status: a flat table of every seam in the current trunk target, sorted by priority and date.
function drawSeamStatus(value) {
  const wrap = el("div");
  const problem = unanswered(value);
  if (problem) { wrap.appendChild(problem); return wrap; }
  if (value.note) wrap.appendChild(el("p", "muted note", scalarText(value.note)));
  const total = value.total || 0;
  const trunk = scalarText(value.trunk || "");
  const summary = el("p", "muted note", total + " seams on " + trunk + ": " +
    Object.entries(value.counts || {}).map(([s, n]) => n + " " + s).join(" · "));
  wrap.appendChild(summary);
  const rows = objects(value.rows);
  if (!rows.length) {
    wrap.appendChild(calm("No seam on the current trunk target."));
    return wrap;
  }
  wrap.appendChild(tableOf(rows, ["seam", "state", "next", "priority", "finished", "since", "why"]));
  return wrap;
}


// The build line: six stations in the order a row travels them, each with its count and what the
// server knows about it, the station where work piles up marked and explained, and under the line one
// tile per box. A station leads to the view that holds its detail. Every string is text.
const LEVELS = new Set(["ok", "amber", "red", "idle", "unknown"]);
const levelOf = value => LEVELS.has(value) ? value : "unknown";

function drawFlow(value) {
  const wrap = el("div");
  const stages = objects(value.stages);
  const pile = value.pile && !isScalar(value.pile) && !Array.isArray(value.pile) ? value.pile : null;
  const line = el("ol", "line");
  for (const stage of stages) {
    const item = el("li", "stage lvl-" + levelOf(stage.level) + (pile && pile.stage === stage.key ? " pile" : ""));
    const link = el("a", "stage-link");
    const view = pageByKey.has(stage.view) ? stage.view : DEFAULT_PAGE;
    link.setAttribute("href", "/ideas/view/" + view);
    link.title = stage.flag ? scalarText(stage.flag) : "Open the " + pageByKey.get(view).title + " view";
    if (typeof link.addEventListener === "function") link.addEventListener("click", ev => {
      if (ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.altKey || ev.button) return;
      ev.preventDefault();
      showPage(view, true);
    });
    link.appendChild(el("span", "stage-name", scalarText(stage.title)));
    const counted = typeof stage.count === "number";
    link.appendChild(el("span", counted ? "stage-count" : "stage-count word",
      counted ? String(stage.count) : scalarText(stage.word || "not read")));
    const facts = el("ul", "stage-facts");
    for (const fact of Array.isArray(stage.facts) ? stage.facts : []) facts.appendChild(el("li", "", scalarText(fact)));
    link.appendChild(facts);
    const days = objects(stage.days);
    if (days.length > 1) link.appendChild(dayBars(days));
    item.appendChild(link);
    line.appendChild(item);
  }
  wrap.appendChild(line);
  if (pile) {
    const note = el("div", "pile-note lvl-" + levelOf(pile.level));
    // The headline and the fix only: the flag's cause and its command are in the Health flags box.
    note.appendChild(el("strong", "", "Work piles up at " + scalarText(pile.title) + ": " + scalarText(pile.headline)));
    if (pile.fix) note.appendChild(el("span", "fix", "Fix: " + scalarText(pile.fix)));
    wrap.appendChild(note);
  } else if (stages.length) {
    wrap.appendChild(el("div", "pile-note lvl-ok", "Nothing piles up: no stage of the line is flagged."));
  }
  const tiles = el("div", "tiles");
  for (const box of objects(value.boxes)) tiles.appendChild(boxTile(box));
  wrap.appendChild(tiles);
  if (!objects(value.boxes).length) wrap.appendChild(calm("No box is known yet: none holds a row, and the fleet was not read."));
  return wrap;
}

// The rows landed on each of the last days, as bars; the newest day is the last bar.
function dayBars(days) {
  const most = Math.max(1, ...days.map(d => (typeof d.landed === "number" && d.landed > 0 ? d.landed : 0)));
  const step = 100 / days.length;
  const children = [{tag: "line", attrs: {x1: "0", y1: "21", x2: "100", y2: "21", class: "track", "stroke-width": "1"}}];
  days.forEach((d, i) => {
    const count = typeof d.landed === "number" && d.landed > 0 ? d.landed : 0;
    const x = String(step * i + step / 2);
    children.push({tag: "line", attrs: {x1: x, y1: "21", x2: x, y2: String(21 - 19 * count / most), class: "fill-good",
      "stroke-width": String(step * 0.6)}, children: [{tag: "title", text: scalarText(d.day) + ": " + count + " landed"}]});
  });
  return svgNode({tag: "svg", attrs: {viewBox: "0 0 100 22", preserveAspectRatio: "none", class: "bar", role: "img",
    "aria-label": "rows landed on each of the last " + days.length + " days"}, children: children});
}

const TILE_ROWS = 4;
const TILE_STATE = {ok: ["ok", "working"], amber: ["warn", "check"], red: ["bad", "stopped"], idle: ["idle", "idle"],
                    unknown: ["unknown", "not read"]};

// One box: its name, a square per row it holds (red: no heartbeat), the rows by name, and what it runs.
function boxTile(box) {
  const level = levelOf(box.level);
  const tile = el("div", "tile lvl-" + level);
  const head = el("div", "tile-head");
  head.appendChild(el("span", "tile-name", scalarText(box.box) + (box.here ? " (this box)" : "")));
  head.appendChild(el("span", "badge badge-" + TILE_STATE[level][0], TILE_STATE[level][1]));
  tile.appendChild(head);
  const claims = objects(box.claims);
  const pips = el("div", "pips");
  for (const claim of claims) {
    const pip = el("span", claim.stale ? "pip stale" : "pip");
    pip.title = scalarText(claim.id) + (claim.heartbeat ? " (heartbeat " + scalarText(claim.heartbeat) + " ago)" : "");
    pips.appendChild(pip);
  }
  tile.appendChild(pips);
  const list = el("ul");
  list.appendChild(el("li", "", claims.length === 1 ? "holds 1 row" : "holds " + claims.length + " rows"));
  for (const claim of claims.slice(0, TILE_ROWS)) {
    const held = el("li", "held", scalarText(claim.id));
    held.title = [claim.id, claim.session, claim.model].filter(Boolean).map(scalarText).join(" \u00b7 ");
    list.appendChild(held);
  }
  if (claims.length > TILE_ROWS) list.appendChild(el("li", "", "and " + (claims.length - TILE_ROWS) + " more"));
  if (typeof box.agents === "number") list.appendChild(el("li", "", box.agents === 1 ? "1 agent running" : box.agents + " agents running"));
  if (box.free) list.appendChild(el("li", "", scalarText(box.free)));
  if (box.idle) list.appendChild(el("li", "", scalarText(box.idle) + " idle with free memory"));
  if (box.gave_back) list.appendChild(el("li", "", scalarText(box.gave_back) + " give-backs on rows still open"));
  if (box.note) list.appendChild(el("li", "tile-note", scalarText(box.note)));
  else if (level === "idle" && box.keeper) list.appendChild(el("li", "tile-note", "keeper: " + scalarText(box.keeper)));
  tile.appendChild(list);
  return tile;
}

// Barriers: one line each, the state first, then what it is, its value and the detail under it.
function drawBarriers(value) {
  const items = objects(value.items);
  if (!items.length) return render(value);
  const wrap = el("div");
  const list = el("ul", "barriers");
  for (const item of items) {
    const row = el("li");
    const state = scalarText(item.state);
    const chip = el("div");
    chip.appendChild(el("span", BADGES.has(state) ? "badge badge-" + state : "badge badge-unknown", state));
    row.appendChild(chip);
    const what = el("div");
    what.appendChild(el("span", "what", scalarText(item.label) + ": " + scalarText(item.value)));
    if (item.detail) what.appendChild(el("span", "detail", scalarText(item.detail)));
    row.appendChild(what);
    list.appendChild(row);
  }
  wrap.appendChild(list);
  return wrap;
}

// A todo-service section that could not be read says so, in the service's own words, and draws nothing else.
function unanswered(value) {
  if (value && value.state === "ok") return null;
  return el("p", "muted note unanswered", scalarText(value && value.why ? value.why : "no answer from the todo service"));
}

// An empty list is a statement: what "nothing here" means, so a blank box never reads as a broken one.
function calm(text) {
  return el("p", "calm", text);
}

const LEVEL_BADGE = {error: "bad", warning: "warn", request: "info"};

// Alarms: the first view. One line per alarm, newest first, and an ack button that tells the todo
// service a person has seen it. Every string is text; the subject an agent wrote cannot become markup.
function drawAlarms(value) {
  const wrap = el("div");
  const problem = unanswered(value);
  if (problem) { wrap.appendChild(problem); return wrap; }
  const rows = objects(value.alarms);
  if (!rows.length) {
    wrap.appendChild(calm("No open alarms. Normal: a box posts one only when something needs a person."));
    return wrap;
  }
  const scroll = el("div", "scroll");
  const sheet = el("table", "alarms");
  const head = sheet.createTHead().insertRow();
  for (const name of ["Level", "When", "From", "Subject", "Detail", ""]) head.appendChild(el("th", "", name));
  const body = sheet.createTBody();
  for (const row of rows) {
    const tr = body.insertRow();
    const level = scalarText(row.level);
    tr.insertCell().appendChild(el("span", "badge badge-" + (LEVEL_BADGE[level] || "unknown"), level));
    const when = tr.insertCell();
    when.className = "when";
    when.appendChild(whenNode(row.at));
    tr.insertCell().textContent = scalarText(row.box) + "/" + scalarText(row.session);
    const subject = tr.insertCell();
    subject.textContent = scalarText(row.subject);
    clipCell(subject);
    const detail = tr.insertCell();
    detail.textContent = scalarText(row.text);
    clipCell(detail);
    const act = tr.insertCell();
    if (row.acked_at) {
      act.appendChild(el("span", "muted", "acked by " + scalarText(row.acked_by)));
    } else {
      const ack = button("ack", "small");
      const note = el("span", "muted", "");
      ack.addEventListener("click", () => ackAlarm(row.alarm_id, ack, note));
      act.appendChild(ack);
      act.appendChild(note);
    }
  }
  scroll.appendChild(sheet);
  wrap.appendChild(scroll);
  return wrap;
}

async function ackAlarm(id, ack, note) {
  ack.disabled = true;
  note.className = "muted";
  note.textContent = " sending";
  try {
    const sent = await post("/ideas/api/alarms/ack", {id: id});
    if (!sent.ok) {
      ack.disabled = false;
      note.className = "error";
      note.textContent = " " + refusal(sent);
      return;
    }
  } catch (err) {
    ack.disabled = false;
    note.className = "error";
    note.textContent = " the ack could not be sent";
    return;
  }
  note.textContent = " acked";
  await load();
}

// Health: the strip's flags, each with its cause, its fix, and the command that does the fix.
function drawHealth(value) {
  const wrap = el("div");
  const flags = objects(value.flags);
  if (!flags.length) {
    wrap.appendChild(calm("Nothing flagged. Normal: every box busy, every claim alive, the seam scan fresh, "
      + "the integrator seated, usage under the line."));
    return wrap;
  }
  for (const flag of flags) {
    const box = el("div", "flag " + (flag.level === "red" ? "red" : "amber"));
    box.appendChild(el("strong", "", scalarText(flag.headline)));
    box.appendChild(el("div", "cause", "cause: " + scalarText(flag.cause)));
    box.appendChild(el("div", "fix", "fix: " + scalarText(flag.fix)));
    if (flag.command) box.appendChild(commandNode(scalarText(flag.command)));
    wrap.appendChild(box);
  }
  return wrap;
}

// Queue: the todo service's open, claimed and blocked rows, as it hands them out.
function drawQueue(value) {
  const wrap = el("div");
  const problem = unanswered(value);
  if (problem) { wrap.appendChild(problem); return wrap; }
  const open = objects(value.open), claimed = objects(value.claimed), blocked = objects(value.blocked);
  const lines = value.lines || {state: "no_answer", why: "Next per line has not been read yet."};
  const chainText = chains => objects(chains).map(c =>
    (Array.isArray(c.rows) ? c.rows.map(scalarText).join(" → ") : "")
      + " (" + scalarText(c.status) + ")").join("; ");
  const chainsById = new Map(objects(lines.blocked).map(r => [r.id, chainText(r.chains)]));
  // Two columns on a wide screen: what is held and what can be taken, then what is next and what waits.
  // What is being worked on starts open; the blocked rows and the chains behind them start shut.
  const columns = el("div", "columns");
  const now = el("div"), later = el("div");
  now.appendChild(fold("claimed", "Claimed (" + claimed.length + ")", claimed.length ? table(claimed.map(r => ({
    P: r.priority, id: r.id, box: r.box, session: r.session, model: r.model,
    heartbeat: typeof r.heartbeat_age_seconds === "number" ? relative(Date.now() - r.heartbeat_age_seconds * 1000, Date.now()) : "",
  })), "claimed") : calm("No claims held. Normal only when the open list is empty too; otherwise no keeper is taking work."), true));
  const byModel = new Map();
  for (const row of open) {
    const model = scalarText(row.model);
    byModel.set(model, (byModel.get(model) || 0) + 1);
  }
  now.appendChild(fold("open", "Open (" + open.length + ")"
    + (byModel.size ? ": " + Array.from(byModel, ([m, n]) => m + " " + n).join(", ") : ""),
    open.length ? tableOf(open.map(r => ({P: r.priority, id: r.id, title: r.title, model: r.model, size: r.size})),
      ["P", "id", "title", "model", "size"], pathKey("open"))
    : calm("No open rows. Normal when every filed row is claimed or landed; file more, or read the blocked list."), true));
  const linesProblem = unanswered(lines);
  const next = el("div");
  if (linesProblem) next.appendChild(linesProblem);
  else {
    const models = objects(lines.models);
    next.appendChild(models.length ? tableOf(models.map(r => ({model: r.model,
      next: objects(r.next).map(n => scalarText(n.id)).join(" → ") || "No eligible rows for this box"})), ["model", "next"], pathKey("lines"))
      : calm("No open model lines."));
  }
  later.appendChild(fold("lines", "Next per line", next, true));
  later.appendChild(fold("blocked", "Blocked (" + blocked.length + ")", blocked.length ? table(blocked.map(r => ({
    P: r.priority, id: r.id, state: r.state, "waits on": Array.isArray(r.blocked_on) && r.blocked_on.length
      ? r.blocked_on.map(scalarText).join(", ") : "the owner", chain: chainsById.get(r.id) || "", why: r.reason, by: r.box, at: r.at,
  })), "blocked") : calm("Nothing blocked."), false));
  if (!linesProblem) {
    const waiting = objects(lines.blocked);
    later.appendChild(fold("chains", "Dependency chains waiting (" + waiting.length + ")", waiting.length
      ? tableOf(waiting.map(r => ({id: r.id, chain: chainText(r.chains)})), ["id", "chain"], pathKey("chains"))
      : calm("No dependency chains waiting."), false));
    const migrations = objects(lines.migrations);
    later.appendChild(fold("migrations", "Migration chain (" + migrations.length + ")", migrations.length
      ? tableOf(migrations.map(r => ({number: r.number, row: r.row, "unblocked by": chainText(r.chains)})),
        ["number", "row", "unblocked by"], pathKey("migrations"))
      : calm("No reserved migrations."), migrations.length > 0));
  }
  columns.appendChild(now);
  columns.appendChild(later);
  wrap.appendChild(columns);
  if (value.read_at) wrap.appendChild(el("p", "muted note", "read from the todo service " + relative(value.read_at * 1000, Date.now())));
  return wrap;
}

// Integration board: every set the todo service holds, its seams, which are ready and why not.
function drawIntegration(value) {
  const wrap = el("div");
  const problem = unanswered(value);
  if (problem) { wrap.appendChild(problem); return wrap; }
  const sets = objects(value.sets);
  const scanned = el("p", "muted note", "");
  scanned.appendChild(document.createTextNode("seams scanned "));
  scanned.appendChild(value.scanned_at ? whenNode(value.scanned_at) : el("span", "", "never"));
  wrap.appendChild(scanned);
  if (!sets.length) {
    wrap.appendChild(calm("No integration set. Normal between batches with nothing pushed."));
    return wrap;
  }
  const seamRows = list => list.map(s => ({
    id: s.id, commits: s.commits, tip: typeof s.tip === "string" ? s.tip.slice(0, 12) : s.tip,
    migration: s.touches_migration ? "yes" : "", why: Array.isArray(s.reasons) ? s.reasons.map(scalarText).join("; ") : "",
    lane: s.lane, timeline: objects(s.timeline).map(t => scalarText(t.step) + " at " + scalarText(t.at)
      + ": waited " + scalarText(t.waited_before) + "s, took "
      + (t.took === null ? "unknown" : scalarText(t.took) + "s")).join("; "),
  }));
  for (const set of sets) {
    const seams = objects(set.seams);
    const ready = seams.filter(s => s.ready === true);
    const waiting = seams.filter(s => s.ready !== true);
    const name = scalarText(set.name || set.set_id || set.id);
    wrap.appendChild(el("h3", "", name + " → " + scalarText(set.target)
      + " (" + scalarText(set.profile) + ", " + scalarText(set.state) + "): " + ready.length + " of " + seams.length + " ready"));
    // What the integrator merges next is open; what is not ready yet, and the set's own record, are shut.
    wrap.appendChild(within(name, () => {
      const part = el("div");
      if (!seams.length) { part.appendChild(calm("No seam in this set.")); return part; }
      part.appendChild(fold("ready", "Ready to merge (" + ready.length + ")", ready.length
        ? table(seamRows(ready), "ready") : calm("No seam is ready. The reasons are under Not ready."), true));
      part.appendChild(fold("waiting", "Not ready (" + waiting.length + ")", waiting.length
        ? table(seamRows(waiting), "waiting") : calm("Every seam in this set is ready."), ready.length === 0));
      const facts = {};
      for (const [key, item] of Object.entries(set)) {
        if (!["seams", "name", "target", "profile", "state", "ready"].includes(key)) facts[key] = item;
      }
      if (Object.keys(facts).length) part.appendChild(fold("record", "The set's record", fields(facts), false));
      return part;
    }));
  }
  return wrap;
}

// Claude usage: each account's share of the week as a bar, the stop line on it.
function drawUsage(value) {
  const rows = objects(value);
  const wrap = el("div");
  if (!rows.length) { wrap.appendChild(calm("No usage reading. Set the usage file in the loops or SESSIONVIEW_WEB_CLAUDE_USAGE.")); return wrap; }
  for (const row of rows) {
    const line = el("div", "usage-row");
    const pct = typeof row.pct === "number" ? Math.max(0, Math.min(100, row.pct)) : null;
    const stop = typeof row.stop === "number" ? Math.max(0, Math.min(100, row.stop)) : null;
    const label = el("div", "", scalarText(row.account) + ": " + (pct === null ? "unknown" : pct + " %")
      + (stop === null ? "" : " of a " + stop + " % stop") + (row.reason ? " — " + scalarText(row.reason) : ""));
    line.appendChild(label);
    const children = [{tag: "line", attrs: {x1: "0", y1: "6", x2: "100", y2: "6", class: "track", "stroke-width": "8"}}];
    if (pct !== null) children.push({tag: "line", attrs: {x1: "0", y1: "6", x2: String(pct), y2: "6",
      class: stop !== null && pct >= stop ? "fill-bad" : stop !== null && pct >= stop - 5 ? "fill-warn" : "fill-good", "stroke-width": "8"}});
    if (stop !== null) children.push({tag: "line", attrs: {x1: String(stop), y1: "0", x2: String(stop), y2: "12", class: "mark", "stroke-width": "1.5"}});
    line.appendChild(svgNode({tag: "svg", attrs: {viewBox: "0 0 100 12", preserveAspectRatio: "none", class: "bar"}, children: children}));
    wrap.appendChild(line);
  }
  return wrap;
}

// Rows landed per day, a bar per day built from the service's data (a day with no landing is a bar of
// zero height and a line of text, never a gap), the median hours from claim to landing in each line.
function drawLanding(value) {
  const wrap = el("div");
  const problem = unanswered(value);
  if (problem) { wrap.appendChild(problem); return wrap; }
  const days = objects(value.days);
  if (!days.length) { wrap.appendChild(calm("No landing reading yet.")); return wrap; }
  const most = Math.max(1, ...days.map(d => (typeof d.landed === "number" && d.landed > 0 ? d.landed : 0)));
  const step = 100 / days.length;
  const children = [{tag: "line", attrs: {x1: "0", y1: "40", x2: "100", y2: "40", class: "track", "stroke-width": "1"}}];
  days.forEach((d, i) => {
    const count = typeof d.landed === "number" && d.landed > 0 ? d.landed : 0;
    const x = String(step * i + step / 2);
    children.push({tag: "line", attrs: {x1: x, y1: "40", x2: x, y2: String(40 - 36 * count / most),
      class: "fill-good", "stroke-width": String(step * 0.6)}, children: [{tag: "title", text: scalarText(d.day) + ": " + count}]});
  });
  wrap.appendChild(svgNode({tag: "svg", attrs: {viewBox: "0 0 100 42", preserveAspectRatio: "none", class: "bar landing"}, children: children}));
  const list = el("ul", "landing-days");
  for (const d of days) {
    const count = typeof d.landed === "number" ? d.landed : 0;
    list.appendChild(el("li", "", scalarText(d.day) + ": " + count + " landed"
      + (typeof d.median_hours === "number" ? ", median " + Math.round(d.median_hours * 10) / 10 + " h" : "")));
  }
  wrap.appendChild(list);
  return wrap;
}

// Settings and help: what is set (never a value that could name a host or a credential), the keys,
// and the metrics this page does not have yet, each with the row that would gather it.
function drawHelp(value) {
  const wrap = el("div");
  wrap.appendChild(el("h3", "", "Keys"));
  wrap.appendChild(render(objects(value.keys)));
  wrap.appendChild(el("h3", "", "Metrics not yet gathered (the todo rows that would gather each)"));
  wrap.appendChild(render(objects(value.metrics_not_yet_gathered)));
  const notJudged = Array.isArray(value.health_checks_not_judged) ? value.health_checks_not_judged : [];
  if (notJudged.length) {
    wrap.appendChild(el("h3", "", "Health checks not judged on this box"));
    wrap.appendChild(render(notJudged));
  }
  wrap.appendChild(el("h3", "", "Settings (environment only; a value is never shown here)"));
  wrap.appendChild(render(objects(value.settings)));
  if (value.doc) wrap.appendChild(el("p", "muted note", "The package map and every setting: " + scalarText(value.doc)));
  return wrap;
}

function drawCriticalPath(value) {
  const wrap = el("div");
  if (value.state !== "ready") {
    wrap.appendChild(el("p", "muted", scalarText(value.reason || "timing data unavailable")));
    return wrap;
  }
  for (const [key, title] of [["gate_runs", "Recent gate runs: critical lane vs even split"],
                               ["slow_suites", "Longest suites"],
                               ["endpoint_latency", "Model endpoint latency"]]) {
    wrap.appendChild(el("h3", "", title));
    wrap.appendChild(render(value[key]));
  }
  wrap.appendChild(el("h3", "", "Recommendations (minutes saved per batch)"));
  for (const rec of objects(value.recommendations)) {
    const box = el("div", "rec");
    const visible = Object.fromEntries(Object.entries(rec).filter(([key]) => key !== "todo" && key !== "todo_text"));
    box.appendChild(render(visible));
    const todo = rec.todo_text === undefined ? rec.todo : rec.todo_text;
    if (todo !== undefined && todo !== "") {
      const details = el("details");
      details.appendChild(el("summary", "", "todo text"));
      details.appendChild(el("pre", "", scalarText(todo)));
      box.appendChild(details);
    }
    wrap.appendChild(box);
  }
  return wrap;
}

// Integrator now: stage, what each lane is doing this second, then one table
// of every suite of the newest gate — finished ones stay on show with their
// duration. The usual time is a link; hovering it lists that suite's last runs.
// Every string is put on the page as text, never as markup.
// One popup for every usual-time link: fixed to the viewport, so a long list
// never lengthens the table or its scroll box. One line per earlier run.
let runpop = null;
function showRuns(ev, row) {
  if (!runpop) { runpop = el("div"); runpop.id = "runpop"; document.body.appendChild(runpop); }
  runpop.replaceChildren(el("div", "head", scalarText(row.suite) + " \u00b7 usual = median of the green runs \u00b7 "
    + scalarText(row.record)));
  const runs = objects(row.runs);
  if (!runs.length) runpop.appendChild(el("div", "", "no earlier run on record"));
  for (const run of runs) {
    const ok = run.result === "passed";
    runpop.appendChild(el("div", ok ? "P" : "F", [scalarText(run.when), scalarText(run.took).padStart(7),
      scalarText(run.result).padEnd(10), scalarText(run.batch)].join("  ")));
  }
  runpop.style.display = "block";
  moveRuns(ev);
}
function moveRuns(ev) {
  if (!runpop) return;
  const box = runpop.getBoundingClientRect();
  let x = ev.clientX + 14, y = ev.clientY + 16;
  if (x + box.width > window.innerWidth - 8) x = Math.max(8, window.innerWidth - box.width - 8);
  if (y + box.height > window.innerHeight - 8) y = Math.max(8, ev.clientY - box.height - 12);
  runpop.style.left = x + "px";
  runpop.style.top = y + "px";
}
function hideRuns() { if (runpop) runpop.style.display = "none"; }

// How many suites of a long pass show before "Show all": every running and red one is among them.
const SUITES_SHOWN = 40;

// The suites a long pass shows first: every running and red one, then the finished ones, then those still
// waiting, up to the cap; they are drawn in the pass's own order (lane by lane).
function firstSuites(rows) {
  const rank = r => r.state === "RUNNING" || r.state === "RED" ? 0 : r.state === "passed" ? 1 : 2;
  const order = rows.map((r, i) => [rank(r), i]).sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  const keep = new Set(order.filter(([n], at) => n === 0 || at < SUITES_SHOWN).map(([, i]) => i));
  return rows.filter((r, i) => keep.has(i));
}

// One suite's row. Every string in it came from a log: it is text, never markup.
function suiteRow(body, r) {
  const tr = body.insertRow();
  tr.insertCell().textContent = scalarText(r.lane);
  tr.insertCell().textContent = scalarText(r.suite);
  const st = tr.insertCell();
  st.className = "st-" + (["RED", "RUNNING", "passed", "pending"].includes(r.state) ? r.state : "pending");
  st.textContent = scalarText(r.state);
  tr.insertCell().textContent = r.took ? scalarText(r.took) : "";
  const usual = tr.insertCell();
  if (r.usual) {
    const tip = el("a", "tip", scalarText(r.usual));
    tip.setAttribute("href", "#");
    tip.addEventListener("click", ev => ev.preventDefault());
    tip.addEventListener("mouseenter", ev => showRuns(ev, r));
    tip.addEventListener("mousemove", moveRuns);
    tip.addEventListener("mouseleave", hideRuns);
    tip.addEventListener("focus", ev => showRuns(ev, r));
    tip.addEventListener("blur", hideRuns);
    usual.appendChild(tip);
  }
  const hist = tr.insertCell();
  hist.className = "strip";
  // A suite with no history (a row read from a gate's report) has an empty strip, not one failed mark.
  for (const ch of (typeof r.strip === "string" ? r.strip : "")) hist.appendChild(el("span", ch === "P" ? "P" : "F", ch === "P" ? "●" : "✖"));
  if (r.record) hist.appendChild(document.createTextNode(" " + scalarText(r.record) + (r.streak ? " · " + scalarText(r.streak) : "")));
  const detail = [r.failed ? "failed: " + scalarText(r.failed) : "", r.note, r.slow, r.progress,
    r.last_output && "output " + scalarText(r.last_output)].filter(Boolean).map(scalarText).join(" · ");
  tr.insertCell().textContent = detail || scalarText(r.result);
}

// The suites of one pass, drawn inside that pass's step. Each Usual time is a link: hovering lists the last runs.
// With a `key` (where it sits on the page) a long list shows its first suites and a button for the rest.
function suiteTable(rows, key) {
  const sheet = el("table", "suites");
  const head = sheet.createTHead().insertRow();
  for (const name of ["Lane", "Suite", "State", "Took", "Usual", "History", "Detail"]) head.appendChild(el("th", "", name));
  const body = sheet.createTBody();
  const scroll = el("div", "scroll");
  scroll.appendChild(sheet);
  const long = Boolean(key) && rows.length > SUITES_SHOWN + ROWS_SLACK;
  if (!long) {
    for (const r of rows) suiteRow(body, r);
    return scroll;
  }
  const foot = el("p", "muted note", "");
  const said = el("span", "", "");
  const more = button("", "small");
  const fill = () => {
    body.replaceChildren();
    const shown = opened.has(key) ? rows : firstSuites(rows);
    for (const r of shown) suiteRow(body, r);
    said.textContent = opened.has(key) ? "" : shown.length + " of " + rows.length
      + " suites shown: every running and red one, then the finished ones ";
    more.textContent = opened.has(key) ? "Show the first " + SUITES_SHOWN + " suites" : "Show all " + rows.length + " suites";
  };
  more.addEventListener("click", () => {
    if (opened.has(key)) opened.delete(key); else opened.add(key);
    fill();
  });
  fill();
  foot.appendChild(said);
  foot.appendChild(more);
  scroll.appendChild(foot);
  return scroll;
}

// A pass's suites in a fold of their own inside its step: the pass running now starts open, a finished one
// starts shut, and its heading says how many suites it ran and how they stand.
function passSuites(st, rows) {
  const running = st.state === "RUNNING";
  const tally = [["passed", "passed"], ["RED", "red"], ["RUNNING", "running"], ["pending", "waiting"]]
    .map(([state, word]) => [rows.filter(r => r.state === state).length, word])
    .filter(([n]) => n > 0).map(([n, word]) => n + " " + word);
  const title = rows.length + (rows.length === 1 ? " suite" : " suites") + (tally.length ? ": " + tally.join(", ") : "");
  return within("suites:" + scalarText(st.step) + (running ? ":now" : ":over"),
    () => fold("suites", title, suiteTable(rows, pathKey("all")), running));
}

// The failed tests of one red fast-test run, in a dialog over the page: one entry per test, with the
// first line of its error. Every string is text. The dialog hangs on the document's body, so the
// refresh that repaints the boxes under it does not shut it.
let failpop = null;
const BRIEF_TESTS = 12;

// What a session needs to start on the red: classify first, then fix or name the seam. Plain text.
function fixBrief(step, seams) {
  const failures = objects(step.failures);
  const total = Number.isInteger(step.failures_total) ? step.failures_total : failures.length;
  const lines = [
    "The fast tests (scripts/skykeep.sh testfast) ended red on the merged batch: " + total + " failed ("
      + scalarText(step.detail) + (step.finished ? ", run ended " + scalarText(step.finished) : "") + ").",
    step.run ? "Log: test-logs/" + scalarText(step.run) + " in the integrator's checkout." : "",
    "First classify the red with .claude/skills/lane-red-triage/SKILL.md: a known flake, the lane's own "
      + "environment, a failure already on the pushed trunk, or a regression from a seam merged in this batch. "
      + "Say the class before changing anything.",
    "A regression: fix it on the seam that caused it, or name that seam so the integrator sends it back. "
      + "A flake or an environment cause: change no application code.",
    "The integrator may already be handling this red: read its steps and the alarms first, and tell it "
      + "what you take, so the work is not done twice.",
    "Failed tests:",
  ].filter(Boolean);
  for (const f of failures.slice(0, BRIEF_TESTS))
    lines.push("- " + scalarText(f.test) + (f.reason ? " -- " + scalarText(f.reason) : ""));
  if (total > BRIEF_TESTS) lines.push("- and " + (total - BRIEF_TESTS) + " more in the log");
  if (seams.length) lines.push("Seams merged in this batch: " + seams.join(", "));
  return lines.join("\n");
}

function closeFailures() {
  if (!failpop) return;
  if (typeof failpop.close === "function" && failpop.open) failpop.close();
  failpop.remove();
  failpop = null;
}

async function copyBrief(text, said, pop) {
  try {
    await navigator.clipboard.writeText(text);
    said.textContent = "copied: paste it into a session";
  } catch (err) {
    // No clipboard here: the brief is shown, selected, to copy by hand.
    if (!pop.brief) {
      pop.brief = el("textarea", "brief", text);
      pop.brief.readOnly = true;
      pop.brief.rows = 12;
      pop.appendChild(pop.brief);
    }
    if (typeof pop.brief.select === "function") pop.brief.select();
    said.textContent = "this browser gave no clipboard: copy the text below";
  }
}

function showFailures(step, seams, anchor) {
  closeFailures();
  const pop = el("dialog", "failpop");
  const failures = objects(step.failures);
  const total = Number.isInteger(step.failures_total) ? step.failures_total : failures.length;
  pop.appendChild(el("h3", "", total + " failed in the fast tests"));
  const about = [step.finished ? "run ended " + scalarText(step.finished) : "",
                 step.run ? "log test-logs/" + scalarText(step.run) : ""].filter(Boolean).join(", ");
  if (about) pop.appendChild(el("p", "muted note", about));
  const list = el("ol", "failures");
  for (const f of failures) {
    const item = el("li");
    item.appendChild(el("code", "", scalarText(f.test)));
    item.appendChild(el("div", f.reason ? "reason" : "reason muted",
      f.reason ? scalarText(f.reason) : "no error line found for this test in the log"));
    list.appendChild(item);
  }
  pop.appendChild(list);
  if (total > failures.length)
    pop.appendChild(el("p", "muted note", failures.length + " of " + total + " shown; the log holds the rest"));
  pop.appendChild(el("p", "note", "To fix it now, copy the brief and give it to a session. A todo row filed now "
    + "could not start before this batch lands: a keeper starts a row only when its brief is on the pushed trunk."));
  const acts = el("div", "acts");
  const copy = button("Copy a fix brief", "small");
  const said = el("span", "muted", "");
  copy.addEventListener("click", () => copyBrief(fixBrief(step, seams), said, pop));
  const shut = button("Close", "small");
  shut.addEventListener("click", closeFailures);
  acts.appendChild(copy);
  acts.appendChild(shut);
  acts.appendChild(said);
  pop.appendChild(acts);
  // A click on the dimmed page around the dialog shuts it, as Escape does.
  pop.addEventListener("click", ev => { if (ev.target === pop) closeFailures(); });
  pop.addEventListener("close", () => { if (failpop === pop) { pop.remove(); failpop = null; } });
  failpop = pop;
  (document.body || anchor).appendChild(pop);
  if (typeof pop.showModal === "function") pop.showModal(); else pop.setAttribute("open", "");
}

// The steps of one part of the integration, one line each, a pass's suites drawn inside its step.
// `seams` are the batch's seams by name, for the brief a red fast-test run offers.
function stepTable(steps, seams) {
  const sheet = el("table");
  const head = sheet.createTHead().insertRow();
  for (const name of ["#", "Step", "State", "Waited before", "Started", "Finished", "Took", "Detail"]) head.appendChild(el("th", "", name));
  const body = sheet.createTBody();
  for (const st of steps) {
    const tr = body.insertRow();
    tr.insertCell().textContent = scalarText(st.n);
    const named = tr.insertCell();
    named.textContent = scalarText(st.step);
    // What the step is, in a sentence, on hover; the same sentences are listed under the steps.
    if (st.what) { named.title = scalarText(st.what); named.className = "explained"; }
    const cell = tr.insertCell();
    const known = ["RED", "RUNNING", "done", "next", "sent-back", "cut off", "reworked"].includes(st.state) ? st.state : "next";
    cell.className = "st-" + known.replace(" ", "-");
    cell.textContent = scalarText(st.state);
    const waited = tr.insertCell();
    waited.textContent = st.gap_before ? scalarText(st.gap_before) : "";
    waited.className = st.gap_before ? "dur st-sent-back" : "dur";
    for (const [field, kind] of [["started", "when"], ["finished", "when"], ["took", "dur"]]) {
      const timed = tr.insertCell();
      timed.className = kind;
      timed.textContent = scalarText(st[field]);
    }
    const detail = tr.insertCell();
    const said = scalarText(st.detail);
    if (objects(st.failures).length) {
      // "4 FAILED" is the way in to the list of those tests and why each failed.
      const hit = /\d+ FAILED/i.exec(said);
      const open = button(hit ? hit[0] : "the failed tests", "linkish");
      open.title = "List the failed tests and why each failed";
      open.addEventListener("click", ev => {
        if (typeof ev.stopPropagation === "function") ev.stopPropagation();
        showFailures(st, seams || [], detail);
      });
      detail.append(hit ? said.slice(0, hit.index) : said + " ");
      detail.appendChild(open);
      if (hit) detail.append(said.slice(hit.index + hit[0].length));
    } else {
      detail.textContent = said;
      clipCell(detail);
    }
    const inside = objects(st.suites);
    if (inside.length) {
      const sub = body.insertRow();
      sub.className = "substeps";
      const nested = sub.insertCell();
      nested.colSpan = 9;
      nested.appendChild(passSuites(st, inside));
    }
  }
  const scroll = el("div", "scroll");
  scroll.appendChild(sheet);
  return scroll;
}

// ---- The run plan: every step of one integration, in order, with its server, times and history.
// Owner, 2026-10-07: the complete list of steps, the earlier runs' times and outcomes, and an ETA that
// counts down each second: mm:ss to the median of the earlier runs, "overdue mm:ss" past it, "--:--"
// with no history. The server sends each running step's elapsed seconds; the page adds the seconds
// since it drew them.
function clockText(seconds) {
  if (typeof seconds !== "number" || !Number.isFinite(seconds)) return "--:--";
  const total = Math.round(Math.abs(seconds));
  const pad = n => String(n).padStart(2, "0");
  return pad(Math.floor(total / 60)) + ":" + pad(total % 60);
}
function etaText(median, elapsed) {
  if (typeof median !== "number" || !Number.isFinite(median)) return "ETA: --:--";
  const left = median - (typeof elapsed === "number" && Number.isFinite(elapsed) ? elapsed : 0);
  return left > -0.5 ? "ETA: " + clockText(left) : "overdue " + clockText(-left);
}
// The ETAs of the running steps on the page now; each redraw starts the list again.
let etaNodes = [];
function tickEtas() {
  const now = Date.now() / 1000;
  for (const e of etaNodes) {
    const said = etaText(e.median, e.elapsed + (now - e.at));
    e.node.textContent = said;
    e.node.className = "eta" + (said.startsWith("overdue") ? " overdue" : "");
  }
}
if (typeof setInterval === "function") setInterval(tickEtas, 1000);

const PLAN_STATES = ["done", "RED", "RUNNING", "next", "skipped", "no record", "left", "none", "blocked"];

// A row's inner list (the merged seams, the re-run suites, the torn-down lanes), one line each, as text.
function planItems(items) {
  const list = el("ul", "plan-items");
  for (const it of items) {
    const li = el("li");
    const st = PLAN_STATES.includes(it.state) ? it.state : "next";
    li.appendChild(el("span", "st-" + st.replace(" ", "-"), scalarText(it.state)));
    li.appendChild(document.createTextNode(" " + scalarText(it.name)));
    if (it.text) li.appendChild(el("span", "muted", " " + scalarText(it.text)));
    list.appendChild(li);
  }
  return list;
}

// One part of the plan as a table: a step per row, its suites or items folded in below it.
function planTable(rows, seams) {
  const sheet = el("table", "plan");
  const head = sheet.createTHead().insertRow();
  for (const name of ["#", "Step", "Srv", "State", "Started", "Finished", "Took", "Usual (earlier runs)", "Detail"])
    head.appendChild(el("th", "", name));
  const body = sheet.createTBody();
  for (const st of rows) {
    const tr = body.insertRow();
    tr.insertCell().textContent = scalarText(st.n);
    const named = tr.insertCell();
    named.className = "step";
    named.appendChild(document.createTextNode(scalarText(st.step)));
    if (st.what) { named.title = scalarText(st.what); named.className = "step explained"; }
    if (st.eta) {
      const running = st.state === "RUNNING" && typeof st.elapsed_s === "number";
      const said = running ? etaText(st.median_s, st.elapsed_s) : scalarText(st.eta);
      const eta = el("span", "eta" + (said.startsWith("overdue") ? " overdue" : ""), said);
      eta.title = running ? "median of the earlier runs minus the time so far, counting down"
        : "the median time of the earlier runs, once it starts";
      named.appendChild(document.createTextNode(" "));
      named.appendChild(eta);
      if (running) etaNodes.push({node: eta, median: st.median_s, elapsed: st.elapsed_s, at: Date.now() / 1000});
    }
    const srv = tr.insertCell();
    srv.className = "srv";
    if (st.server) srv.appendChild(el("span", "srv-tag", scalarText(st.server)));
    const cell = tr.insertCell();
    const known = PLAN_STATES.includes(st.state) ? st.state : "next";
    cell.className = "st-" + known.replace(" ", "-");
    cell.textContent = scalarText(st.state);
    for (const [field, kind] of [["started", "when"], ["finished", "when"], ["took", "dur"], ["history", "hist"]]) {
      const timed = tr.insertCell();
      timed.className = kind;
      timed.textContent = st[field] ? scalarText(st[field]) : "";
    }
    const detail = tr.insertCell();
    const said = scalarText(st.detail);
    if (objects(st.failures).length) {
      const hit = /\d+ FAILED/i.exec(said);
      const open = button(hit ? hit[0] : "the failed tests", "linkish");
      open.title = "List the failed tests and why each failed";
      open.addEventListener("click", ev => {
        if (typeof ev.stopPropagation === "function") ev.stopPropagation();
        showFailures(st, seams || [], detail);
      });
      detail.append(hit ? said.slice(0, hit.index) : said + " ");
      detail.appendChild(open);
      if (hit) detail.append(said.slice(hit.index + hit[0].length));
    } else {
      detail.textContent = said;
      clipCell(detail);
    }
    const suites = objects(st.suites);
    const items = objects(st.items);
    if (suites.length || items.length) {
      const sub = body.insertRow();
      sub.className = "substeps";
      const nested = sub.insertCell();
      nested.colSpan = 9;
      if (suites.length) nested.appendChild(passSuites(st, suites));
      if (items.length) nested.appendChild(within("items:" + scalarText(st.key),
        () => fold("items", items.length + (items.length === 1 ? " item" : " items"), planItems(items),
          st.state === "RUNNING" || items.length <= 6)));
    }
  }
  const scroll = el("div", "scroll");
  scroll.appendChild(sheet);
  return scroll;
}

// The whole plan: a head line, then each part in its own fold. A finished part starts shut with how it
// went beside its name; the part running now and every part still to come start open.
function runPlanView(plan, seams) {
  const wrap = el("div", "plan-view");
  etaNodes = [];
  const head = plan.head && !isScalar(plan.head) ? plan.head : {};
  const lines = [];
  if (head.state === "running") {
    lines.push("Integration run" + (head.batch ? " " + scalarText(head.batch) : "") + ": started "
      + scalarText(head.started) + ", running " + scalarText(head.running_for));
  } else if (head.state === "between") {
    lines.push("Between runs: the last" + (head.last_batch ? " (" + scalarText(head.last_batch) + ")" : "")
      + " ended " + scalarText(head.last_end) + (head.landed ? ", landed" : ", nothing landed")
      + ". Below: the next run's steps.");
  }
  if (plan.remaining) lines.push("Steps left take about " + scalarText(plan.remaining) + " by their earlier runs"
    + (plan.remaining_unknown ? " (" + scalarText(plan.remaining_unknown) + " step(s) have no history)" : "")
    + "; fixups to come add to it.");
  if (plan.whole_run) lines.push("A whole run: " + scalarText(plan.whole_run) + ".");
  for (const text of lines) wrap.appendChild(el("p", "plan-head", text));
  if (head.last_line) wrap.appendChild(el("p", "muted note", "Integrator log" + (head.last_at ? " " + scalarText(head.last_at) : "")
    + ": " + scalarText(head.last_line) + (head.last_next ? " → next: " + scalarText(head.last_next) : "")));
  if (head.quiet) wrap.appendChild(el("p", "error", scalarText(head.quiet)));
  for (const group of objects(plan.groups)) {
    const rows = objects(group.rows);
    if (!rows.length) continue;
    const open = group.state !== "done" || group.key === "fixup";
    wrap.appendChild(fold("plan-" + scalarText(group.key), scalarText(group.title) + " (" + rows.length + ")",
      planTable(rows, seams), open, scalarText(group.summary)));
  }
  for (const text of Array.isArray(plan.notes) ? plan.notes : []) wrap.appendChild(el("p", "muted note", scalarText(text)));
  return wrap;
}

// The batch being integrated now, as a list: the inset at the top right of Integrator now. The server
// says where the list comes from, and says so when it is the next batch's seams and not this one's.
const INSET_ROWS = 12;
function batchInset(now) {
  const box = el("aside", "batch-now");
  box.appendChild(el("h3", "", scalarText(now.headline)));
  if (now.source) box.appendChild(el("p", "muted", scalarText(now.source)));
  if (now.next) box.appendChild(el("p", "label", scalarText(now.next)));
  const seams = objects(now.seams);
  if (!seams.length) return box;
  const key = pathKey("batch-now");
  const long = seams.length > INSET_ROWS + ROWS_SLACK;
  const list = el("ul");
  const more = button("", "small");
  const fill = () => {
    list.replaceChildren();
    for (const s of (long && !opened.has(key) ? seams.slice(0, INSET_ROWS) : seams)) {
      const item = el("li");
      item.appendChild(document.createTextNode(scalarText(s.seam)));
      if (s.note) item.appendChild(el("span", "muted", " " + scalarText(s.note)));
      list.appendChild(item);
    }
    more.textContent = opened.has(key) ? "Show the first " + INSET_ROWS : "Show all " + seams.length;
  };
  more.addEventListener("click", () => {
    if (opened.has(key)) opened.delete(key); else opened.add(key);
    fill();
  });
  fill();
  box.appendChild(list);
  if (long) box.appendChild(more);
  return box;
}

function drawIntegrator(v) {
  const outer = el("div");
  // The top: what the integrator is doing, and to its right the seams of the batch it is doing it to.
  const top = el("div", "now-top");
  let wrap = el("div", "now-lines");
  top.appendChild(wrap);
  outer.appendChild(top);
  const now = v.batch_now && !isScalar(v.batch_now) && !Array.isArray(v.batch_now) ? v.batch_now : null;
  if (now) top.appendChild(batchInset(now));
  const line = (label, text) => {
    const p = el("p", "", "");
    p.appendChild(el("strong", "", label + ": "));
    p.appendChild(document.createTextNode(scalarText(text)));
    return p;
  };
  wrap.appendChild(line("Stage", v.stage));
  if (v.stage_is) wrap.appendChild(el("p", "muted note", scalarText(v.stage_is)));
  const gate = v.gate && !isScalar(v.gate) && !Array.isArray(v.gate) ? v.gate : {};
  const ledger = v.last_ledger ? " \u00b7 last ledger " + scalarText(v.last_ledger.name || "Batch" + scalarText(v.last_ledger.number)) + " at " + scalarText(v.last_ledger.at) : "";
  // The top names the box its gate runs on; a top older than the ledger says so instead of passing as current.
  const where = v.gate_box && gate.state ? " on " + scalarText(v.gate_box) : "";
  wrap.appendChild(line("Batch", v.gate_stale ? scalarText(v.gate_stale) + ledger
    : scalarText(v.batch) + (gate.state ? " (" + gate.state + where + ", started " + scalarText(gate.started)
    + (gate.running_for && gate.running_for !== "-" ? ", running " + scalarText(gate.running_for) : "") + ")" : "")
    + ledger));
  const gateBoxes = Array.isArray(v.gate_boxes) ? v.gate_boxes : [];
  if (gateBoxes.length) wrap.appendChild(line("Gate boxes", gateBoxes.map(scalarText).join(" \u00b7 ")));
  if (v.merging_now) wrap.appendChild(line("Merging now", v.merging_now));
  const queue = v.next_batch && !isScalar(v.next_batch) ? objects(v.next_batch.waiting) : [];
  if (v.next_batch) {
    const nb = el("div", "nowbar");
    nb.appendChild(el("strong", "", "Next batch: not started. " + queue.length + " seam(s) pushed and waiting"));
    wrap.appendChild(nb);
    // The seams themselves fold under the bar, the longest wait first as the server sent them; a short list starts open.
    if (queue.length) wrap.appendChild(fold("waiting", "Seams waiting (" + queue.length + ")",
      tableOf(queue.map(q => ({seam: q.seam, waiting: q.waiting, why: q.why})), ["seam", "waiting", "why"], pathKey("waiting")),
      queue.length <= ROWS_SHOWN));
  }
  const doing = objects(gate.now);
  if (doing.length) {
    const bar = el("div", "nowbar");
    bar.appendChild(el("strong", "", "Doing right now"));
    for (const d of doing) {
      const parts = d.lane ? [scalarText(d.phase) + " pass (" + scalarText(d.meaning) + ")", "lane " + scalarText(d.lane),
        scalarText(d.suite), "elapsed " + scalarText(d.elapsed)] : [scalarText(d.suite)];
      for (const k of ["progress", "last_output", "last_line"]) if (d[k]) parts.push(scalarText(d[k]));
      bar.appendChild(el("div", "", parts.join(" \u00b7 ")));
    }
    wrap.appendChild(bar);
  }
  if (gate.state === "running") wrap.appendChild(line("ETA", "live phase ends in " + scalarText(gate.eta_live_phase)
    + " (about " + scalarText(gate.finishes_about) + "); pace " + scalarText(gate.pace_vs_usual) + " of usual"));
  const unit = v.unit_lane && !isScalar(v.unit_lane) ? v.unit_lane : null;
  if (unit) wrap.appendChild(line("Fast tests (testfast)", scalarText(unit.state) + (unit.last
    ? " \u00b7 last: " + scalarText(unit.last.result) + " in " + scalarText(unit.last.took) + " at " + scalarText(unit.last.finished)
    : "")));
  // Without the inset (an older server, a saved snapshot) the merged seams stay on one line here.
  const seams = objects(v.seams_in_batch);
  if (seams.length && !now) wrap.appendChild(line("Seams in batch (" + seams.length + ")",
    seams.map(s => scalarText(s.seam) + " @" + scalarText(s.merged)).join("; ")));
  const named = (now && now.state !== "next" ? objects(now.seams) : seams).map(s => scalarText(s.seam));
  wrap = outer;
  const plan = v.plan && !isScalar(v.plan) && !Array.isArray(v.plan) ? v.plan : null;
  if (plan && plan.problem) wrap.appendChild(el("p", "error", scalarText(plan.problem)));
  if (plan && objects(plan.groups).length) {
    wrap.appendChild(el("h3", "", "Integration steps: every step of the run, in order"));
    wrap.appendChild(runPlanView(plan, named));
    if (v.board_note) wrap.appendChild(el("p", "muted note", scalarText(v.board_note)));
  }
  const steps = plan && objects(plan.groups).length ? [] : objects(v.steps);
  if (steps.length) {
    wrap.appendChild(el("h3", "", "Integration steps (kept: finished steps stay, with start and end)"));
    // Three parts, each folding: the steps that are over (shut), the step running now (open), the
    // steps still to come (shut). The finished part says beside its name how it went.
    const running = steps.filter(st => st.state === "RUNNING");
    const coming = steps.filter(st => st.state === "next");
    const ended = steps.filter(st => st.state !== "RUNNING" && st.state !== "next");
    // A finished gate pass keeps its suites in sight: its own fold, open, each pass's suite list shut with
    // its count (owner, 2026-10-07: the wave pass's suites must be listed, not summarised away).
    const passes = ended.filter(st => objects(st.suites).length);
    const over = ended.filter(st => !objects(st.suites).length);
    const reds = over.filter(st => st.state === "RED").length;
    const last = over.length ? over[over.length - 1] : null;
    if (over.length) wrap.appendChild(fold("steps-over", "Finished (" + over.length + ")", stepTable(over, named), false,
      [reds ? reds + " red" : "none red", last && last.finished ? "last ended " + scalarText(last.finished) : ""]
        .filter(Boolean).join(", ")));
    if (passes.length) {
      const ran = passes.reduce((n, st) => n + objects(st.suites).length, 0);
      const red = passes.reduce((n, st) => n + objects(st.suites).filter(r => r.state === "RED").length, 0);
      wrap.appendChild(fold("steps-passes", "Gate passes finished (" + passes.length + ")", stepTable(passes, named), true,
        ran + " suites, " + (red ? red + " red" : "none red")));
    }
    if (running.length) wrap.appendChild(fold("steps-now", "Running now (" + running.length + ")", stepTable(running, named), true));
    if (coming.length) wrap.appendChild(fold("steps-next", "Still to come (" + coming.length + ")", stepTable(coming, named), false));
  }
  if (v.board_note && !(plan && objects(plan.groups).length)) wrap.appendChild(el("p", "muted note", scalarText(v.board_note)));
  // With a plan, "What each step is" lists the plan's own steps (one line per kind of step).
  const planned = [];
  for (const group of plan ? objects(plan.groups) : []) for (const st of objects(group.rows))
    if (st.what && !planned.some(m => m.step === st.step)) planned.push({step: st.step, is: st.what});
  const meanings = planned.length ? planned : objects(v.step_meanings);
  if (meanings.length) {
    const list = el("ul", "meanings");
    for (const m of meanings) {
      const item = el("li");
      item.appendChild(el("strong", "", scalarText(m.step)));
      item.appendChild(document.createTextNode(": " + scalarText(m.is)));
      list.appendChild(item);
    }
    wrap.appendChild(fold("steps-what", "What each step is", list, false));
  }
  if (v.steps_problem) wrap.appendChild(el("p", "error", scalarText(v.steps_problem)));
  for (const text of [...(Array.isArray(gate.rework) ? gate.rework : []).map(t => "Rework: " + scalarText(t)),
                      ...(Array.isArray(gate.notes) ? gate.notes : []), ...(Array.isArray(v.notes) ? v.notes : [])])
    wrap.appendChild(el("p", "muted note", scalarText(text)));
  return outer;
}

// Summaries: one button per topic. Pressing it asks the server, which asks
// the model endpoint on its own time; the answer comes back with a later
// refresh, and every word of it is put on the page as text.
function drawSummaries(value) {
  const wrap = el("div");
  wrap.appendChild(el("p", "muted note", "asked of " + scalarText(value.model) + " at "
    + scalarText(value.endpoint) + ", only when a button is pressed"));
  for (const topic of objects(value.topics)) {
    const pending = topic.state === "pending";
    const ask = button(pending ? "Asking..." : "Summarise " + scalarText(topic.title));
    ask.disabled = pending;
    const note = el("p", "muted note", scalarText(topic.note));
    ask.addEventListener("click", () => askSummary(String(topic.topic), ask, note));
    wrap.appendChild(ask);
    if (topic.state === "ok" && typeof topic.summary === "string" && topic.summary) {
      wrap.appendChild(el("pre", "summary", topic.summary));
    }
    wrap.appendChild(note);
  }
  return wrap;
}

async function askSummary(topic, ask, note) {
  ask.disabled = true;
  ask.textContent = "Asking...";
  try {
    const sent = await post("/ideas/api/summary", {topic: topic});
    if (!sent.ok) {
      ask.disabled = false;
      note.className = "error note";
      note.textContent = refusal(sent);
      return;
    }
  } catch (err) {
    ask.disabled = false;
    note.className = "error note";
    note.textContent = "the ask could not be sent";
    return;
  }
  await load();
}

// Disk cleanup: Preview asks for a plan, which deletes nothing and names
// every directory a run would delete; only the items ticked are run, and
// only after a confirmation. A plan is used once.
function drawCleanup(value) {
  const wrap = el("div");
  wrap.appendChild(fields(value));
  const preview = button("Preview cleanup");
  const out = el("div", "plan");
  preview.addEventListener("click", () => previewCleanup(preview, out));
  wrap.appendChild(preview);
  wrap.appendChild(out);
  return wrap;
}

async function previewCleanup(preview, out) {
  preview.disabled = true;
  out.replaceChildren(el("p", "muted", "asking for a plan; a preview deletes nothing"));
  try {
    const sent = await post("/ideas/api/cleanup/plan", {});
    out.replaceChildren(sent.ok ? planView(sent.answer, out) : el("p", "error", refusal(sent)));
  } catch (err) {
    out.replaceChildren(el("p", "error", "the plan could not be fetched"));
  } finally {
    preview.disabled = false;
  }
}

function planView(plan, out) {
  const wrap = el("div");
  const items = objects(plan.items);
  const ticked = new Set();
  const run = button("Delete the ticked items", "danger");
  run.disabled = true;
  wrap.appendChild(el("p", "", items.length
    ? "A run deletes, of the items you tick, exactly the directories listed under each ("
      + scalarText(plan.total) + " in all; this plan expires in " + scalarText(plan.expires_in) + "):"
    : "Nothing is safe to delete right now."));
  const list = el("ul");
  for (const item of items) {
    const id = String(item.id);
    const entry = el("li");
    const label = el("label");
    const tick = el("input");
    tick.type = "checkbox";
    tick.addEventListener("change", () => {
      if (tick.checked) ticked.add(id); else ticked.delete(id);
      run.disabled = ticked.size === 0;
    });
    label.appendChild(tick);
    label.appendChild(document.createTextNode(" " + scalarText(item.kind) + " · " + scalarText(item.path)
      + " · " + scalarText(item.size_text)));
    entry.appendChild(label);
    entry.appendChild(el("div", "muted", scalarText(item.why)));
    const targets = el("ul");
    for (const target of Array.isArray(item.targets) ? item.targets : []) {
      targets.appendChild(el("li", "", "deletes " + scalarText(target)));
    }
    entry.appendChild(targets);
    list.appendChild(entry);
  }
  wrap.appendChild(list);
  if (items.length) wrap.appendChild(run);
  const held = objects(plan.held);
  if (held.length) {
    wrap.appendChild(el("h3", "", "Held back, and why"));
    wrap.appendChild(render(held));
  }
  run.addEventListener("click", () => runCleanup(plan.plan, items.filter(i => ticked.has(String(i.id))), out));
  return wrap;
}

async function runCleanup(planId, chosen, out) {
  if (!chosen.length) return;
  const targets = chosen.reduce((n, i) => n + (Array.isArray(i.targets) ? i.targets.length : 0), 0);
  const question = "Delete " + chosen.length + " item(s), " + targets + " director(ies)? Each is checked "
    + "again first, and if one no longer passes nothing is deleted.";
  // No way to ask is no consent: nothing is sent.
  if (typeof window.confirm !== "function" || !window.confirm(question)) return;
  out.replaceChildren(el("p", "muted", "deleting"));
  try {
    const sent = await post("/ideas/api/cleanup/run", {plan: planId, items: chosen.map(i => String(i.id))});
    out.replaceChildren(runView(sent));
  } catch (err) {
    out.replaceChildren(el("p", "error", "the run could not be sent; preview again"));
  }
  await load();
}

function runView(sent) {
  const wrap = el("div");
  const answer = sent.answer;
  if (!sent.ok) {
    wrap.appendChild(el("p", "error", refusal(sent)));
    if (Array.isArray(answer.problems) && answer.problems.length) wrap.appendChild(render(answer.problems));
  }
  wrap.appendChild(el("h3", "", "Deleted"));
  wrap.appendChild(render(objects(answer.deleted)));
  if (objects(answer.failed).length) {
    wrap.appendChild(el("h3", "", "Not deleted"));
    wrap.appendChild(render(objects(answer.failed)));
  }
  wrap.appendChild(el("p", "muted", "a plan is used once: preview again for another run"));
  return wrap;
}
