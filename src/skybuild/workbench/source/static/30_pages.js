// ---- views ------------------------------------------------------------------
// The rail names the views; /view/<name> is one of them. The state is fetched once
// for every view, and a view shows its own sections of it: switching views repaints
// from the last state, nothing is fetched again.
const pageByKey = new Map(PAGES.map(p => [p.key, p]));
const PAGE_KEY = "workbench.page";
let lastState = null;
let currentPage = pageFromPath();

// The view the path names; / shows the one last shown in this browser, else the default; an
// unknown name shows the default. A page with no location at all (a harness) shows every section.
function pageFromPath() {
  if (typeof window.__PAGE__ === "string") return pageByKey.has(window.__PAGE__) ? window.__PAGE__ : DEFAULT_PAGE;
  if (typeof location === "undefined" || !location || typeof location.pathname !== "string") return "all";
  const named = /^\/workbench\/views\/([a-z]+)$/.exec(location.pathname);
  if (named) return pageByKey.has(named[1]) ? named[1] : DEFAULT_PAGE;
  let remembered = null;
  try { remembered = window.localStorage.getItem(PAGE_KEY); } catch (err) {}
  return remembered && pageByKey.has(remembered) ? remembered : DEFAULT_PAGE;
}

// The sections this view shows, in the view's own order (the Everything view: every one, in title order).
function visibleSections(keys) {
  const page = pageByKey.get(currentPage);
  if (!page || page.sections === null) return keys;
  const have = new Set(keys);
  return page.sections.filter(key => have.has(key));
}

function baseTitle() {
  const page = pageByKey.get(currentPage);
  if (page && page.key === "boxes") return "Fleet Members";
  return page && page.key !== "all" ? page.title + " – Build status" : "Build status";
}

function showPage(key, push) {
  if (!pageByKey.has(key)) key = DEFAULT_PAGE;
  currentPage = key;
  try { window.localStorage.setItem(PAGE_KEY, key); } catch (err) {}
  if (push && typeof history !== "undefined" && history && typeof history.pushState === "function") {
    try { history.pushState({page: key}, "", "/workbench/views/" + key); } catch (err) {}
  }
  paintRail();
  if (lastState) paint(lastState);
  else if (key === "boxes") paint({repo: "Tailnet fleet", generated_text: "Fleet data refreshes only when requested", sections: {}});
  if (key === "boxes") void loadFleetCache();
  if (typeof window.scrollTo === "function") { try { window.scrollTo(0, 0); } catch (err) {} }
}

function paintRail() {
  const page = pageByKey.get(currentPage);
  if (viewTitle) viewTitle.textContent = page ? page.title : "Build status";
  const asks = document.getElementById("asks");
  if (asks) asks.textContent = page ? page.asks : "";
  const pause = document.getElementById("pause");
  if (pause) pause.hidden = document.body.dataset.fleetInventory === "enabled" && Boolean(page && page.key === "boxes");
  if (!rail || !rail.children) return;
  for (const link of rail.children) {
    if (typeof link.getAttribute !== "function") continue;
    const key = link.getAttribute("data-page");
    link.className = key === currentPage ? "rail-link current" : "rail-link";
    if (key === currentPage) link.setAttribute("aria-current", "page"); else link.removeAttribute("aria-current");
  }
}

if (rail && typeof rail.addEventListener === "function") {
  rail.addEventListener("click", ev => {
    const link = ev.target && typeof ev.target.closest === "function" ? ev.target.closest("a[data-page]") : null;
    if (!link || ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.altKey || ev.button) return;
    ev.preventDefault();
    showPage(link.getAttribute("data-page"), true);
  });
}
window.addEventListener("popstate", () => {
  currentPage = pageFromPath();
  paintRail();
  if (lastState) paint(lastState);
  if (currentPage === "boxes") void loadFleetCache();
});

// ---- the health strip, on every view -----------------------------------------
function paintHealth(section) {
  if (!healthBar || !healthText) return;
  const health = section && !isScalar(section) && !Array.isArray(section) ? section : null;
  const state = health && ["green", "amber", "red"].includes(health.state) ? health.state : "unknown";
  healthBar.className = "health " + state;
  healthText.textContent = health ? scalarText(health.headline) : "health: not computed by this server";
  healthBar.title = health && health.count ? health.count + " flag(s); the Home view lists each with its fix" : "";
}

// ---- the refresh: a visible count-down, and a pause ----------------------------
let paused = false;
let nextAt = 0;

function tickCountdown() {
  if (!countdown) return;
  if (currentPage === "boxes" && document.body.dataset.fleetInventory === "enabled") {
    countdown.textContent = "fleet updates only on manual refresh";
    return;
  }
  if (paused) { countdown.textContent = "refresh paused"; return; }
  const left = Math.max(0, Math.round((nextAt - Date.now()) / 1000));
  countdown.textContent = nextAt ? "refresh in " + left + " s" : "";
}
if (typeof setInterval === "function" && countdown) setInterval(tickCountdown, 1000);

function setPaused(on) {
  paused = on;
  const pauseButton = document.getElementById("pause");
  if (pauseButton) pauseButton.textContent = paused ? "resume" : "pause";
  tickCountdown();
}

// ---- what changed since you looked --------------------------------------------
// Sections whose content changed since the page was last looked at: the list grows while the
// tab is hidden, and a click or a key on the page clears it.
const changedKeys = new Set();
let looking = true;

function noteChanges(changed) {
  if (!changedLine) return;
  for (const key of changed) changedKeys.add(key);
  if (looking && changed.length === 0) return;
  changedLine.textContent = changedKeys.size
    ? "changed since you looked: " + Array.from(changedKeys).map(titleOf).join(", ") : "";
}

function clearChanges() {
  changedKeys.clear();
  if (changedLine) changedLine.textContent = "";
}

// ---- the jump list ("/") ---------------------------------------------------------
const palette = document.getElementById("palette");
const paletteInput = document.getElementById("palette-input");
const paletteList = document.getElementById("palette-list");
let paletteChoice = 0;

function fillPalette(filter) {
  if (!paletteList) return;
  const needle = String(filter || "").trim().toLowerCase();
  const hits = PAGES.filter(p => !needle || p.title.toLowerCase().includes(needle) || p.key.includes(needle)
    || p.asks.toLowerCase().includes(needle));
  paletteChoice = Math.min(paletteChoice, Math.max(0, hits.length - 1));
  paletteList.replaceChildren();
  hits.forEach((p, i) => {
    const item = el("li", i === paletteChoice ? "chosen" : "");
    item.appendChild(el("strong", "", p.title));
    item.appendChild(el("span", "muted", " " + p.asks));
    item.addEventListener("click", () => { closePalette(); showPage(p.key, true); });
    paletteList.appendChild(item);
  });
  paletteList.hits = hits;
}

function openPalette() {
  if (!palette || !paletteInput) return;
  palette.hidden = false;
  paletteInput.value = "";
  paletteChoice = 0;
  fillPalette("");
  paletteInput.focus();
}

function closePalette() {
  if (palette) palette.hidden = true;
}

if (paletteInput) {
  paletteInput.addEventListener("input", () => { paletteChoice = 0; fillPalette(paletteInput.value); });
  paletteInput.addEventListener("keydown", ev => {
    const hits = paletteList && paletteList.hits ? paletteList.hits : [];
    if (ev.key === "Escape") { closePalette(); return; }
    if (ev.key === "ArrowDown") { paletteChoice = Math.min(hits.length - 1, paletteChoice + 1); fillPalette(paletteInput.value); ev.preventDefault(); }
    if (ev.key === "ArrowUp") { paletteChoice = Math.max(0, paletteChoice - 1); fillPalette(paletteInput.value); ev.preventDefault(); }
    if (ev.key === "Enter" && hits[paletteChoice]) { closePalette(); showPage(hits[paletteChoice].key, true); }
  });
}

// ---- keys, the tool buttons, print view ---------------------------------------------
function installTools() {
  const pauseButton = document.getElementById("pause");
  if (pauseButton) pauseButton.addEventListener("click", () => setPaused(!paused));
  const densityButton = document.getElementById("density");
  const labelDensity = () => { if (densityButton) densityButton.textContent = comfortable ? "compact" : "comfortable"; };
  if (densityButton) densityButton.addEventListener("click", () => {
    comfortable = !comfortable;
    try { window.localStorage.setItem(DENSITY_KEY, comfortable ? "comfortable" : "compact"); } catch (err) {}
    applyDensity();
    labelDensity();
  });
  labelDensity();
  applyDensity();
  // The theme button steps through system, light and dark, and says which one is on.
  const themeButton = document.getElementById("theme");
  const labelTheme = () => { if (themeButton) themeButton.textContent = "theme: " + theme; };
  if (themeButton) themeButton.addEventListener("click", () => {
    theme = THEMES[(THEMES.indexOf(theme) + 1) % THEMES.length];
    try { window.localStorage.setItem(THEME_KEY, theme); } catch (err) {}
    applyTheme();
    labelTheme();
  });
  labelTheme();
  const printButton = document.getElementById("print");
  if (printButton) printButton.addEventListener("click", () => {
    if (document.body && document.body.classList) document.body.classList.toggle("print");
    printButton.textContent = document.body && document.body.classList && document.body.classList.contains("print")
      ? "screen view" : "print view";
  });
  if (typeof document.addEventListener !== "function") return;
  document.addEventListener("keydown", ev => {
    const target = ev.target;
    const typing = target && /^(input|textarea|select)$/i.test(String(target.tagName || "")) || (target && target.isContentEditable);
    if (typing || ev.metaKey || ev.ctrlKey || ev.altKey) return;
    if (ev.key === "/") { ev.preventDefault(); openPalette(); }
    else if (ev.key === "Escape") closePalette();
    else if (ev.key === "p") setPaused(!paused);
    else if (ev.key === "d" && densityButton) densityButton.click();
  });
  document.addEventListener("click", clearChanges);
  document.addEventListener("visibilitychange", () => {
    looking = document.visibilityState !== "hidden";
    if (looking) noteChanges([]);
  });
}

// A command beside a flag: shown as code, with a button that copies it where the browser allows.
function commandNode(text) {
  const wrap = el("div", "command");
  wrap.appendChild(el("code", "", text));
  const copy = button("copy", "small");
  copy.addEventListener("click", async () => {
    try {
      if (typeof navigator === "undefined" || !navigator.clipboard) throw new Error("no clipboard");
      await navigator.clipboard.writeText(text);
      copy.textContent = "copied";
    } catch (err) {
      copy.textContent = "select the text and copy it";
    }
  });
  wrap.appendChild(copy);
  return wrap;
}
