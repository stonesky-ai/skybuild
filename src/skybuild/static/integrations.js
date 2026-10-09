"use strict";

(() => {
  const byId = id => document.getElementById(id);
  let rows = [];
  let candidates = [];
  let selectedIds = new Set();
  let selectionMode = false;
  let sortKey = "activity";
  let sortDirection = -1;

  function sortValue(row, key) {
    if (key === "task") return `${row.task.title} ${row.task.id}`.toLowerCase();
    if (key === "worktree") return `${row.path} ${row.branch}`.toLowerCase();
    if (key === "activity") return row.last_activity_epoch || 0;
    if (key === "commits") return row.commit_count || 0;
    if (key === "bundle") return `${row.bundle_branch || ""} ${row.bundle_target || ""}`.toLowerCase();
    if (key === "rebases") return row.rebase_count === null ? -1 : row.rebase_count;
    if (key === "state") return row.task_state.toLowerCase();
    return "";
  }

  function render() {
    const body = byId("worktree-rows");
    const collator = new Intl.Collator(undefined, {numeric: true, sensitivity: "base"});
    const sorted = [...rows].sort((left, right) => {
      const a = sortValue(left, sortKey), b = sortValue(right, sortKey);
      const order = typeof a === "number" && typeof b === "number" ? a - b : collator.compare(String(a), String(b));
      return order * sortDirection;
    });
    body.replaceChildren();
    for (const row of sorted) {
      const tr = document.createElement("tr");
      const task = document.createElement("td");
      const title = document.createElement("strong"); title.textContent = row.task.title;
      task.append(title);
      if (row.task.id) { const id = document.createElement("code"); id.textContent = row.task.id; task.append(document.createElement("br"), id); }
      const basis = document.createElement("small"); basis.textContent = row.task.basis; task.append(document.createElement("br"), basis);
      tr.append(task);

      const worktree = document.createElement("td");
      const branch = document.createElement("strong"); branch.textContent = row.branch;
      const path = document.createElement("code"); path.textContent = row.path;
      const head = document.createElement("small"); head.textContent = `HEAD ${row.head.slice(0, 12)}`;
      worktree.append(branch, document.createElement("br"), path, document.createElement("br"), head);
      tr.append(worktree);

      const activity = document.createElement("td"); activity.className = "activity-cell";
      if (!row.commits.length) activity.textContent = "No commit data available.";
      else {
        const list = document.createElement("ul");
        for (const commit of row.commits) {
          const item = document.createElement("li");
          const sha = document.createElement("code"); sha.textContent = commit.sha;
          const time = document.createElement("time"); time.dateTime = commit.datetime; time.textContent = commit.datetime;
          const author = document.createElement("span"); author.textContent = commit.author;
          const message = document.createElement("span"); message.className = "commit-message"; message.textContent = commit.message;
          item.append(sha, document.createTextNode(" · "), time, document.createTextNode(" · "), author,
            document.createTextNode(" — "), message);
          list.append(item);
        }
        activity.append(list);
        if (row.commits.length < row.commit_count) {
          const omitted = document.createElement("small");
          omitted.textContent = `Showing latest ${row.commits.length} of ${row.commit_count} task commits`;
          activity.append(document.createElement("br"), omitted);
        }
      }
      tr.append(activity);

      const commits = document.createElement("td"); commits.textContent = String(row.commit_count ?? 0);
      commits.title = `Unique commits against ${row.comparison_base || "unknown comparison base"}`;
      tr.append(commits);

      const bundle = document.createElement("td");
      if (row.bundle_branch) {
        const name = document.createElement("strong"); name.textContent = row.bundle_branch; bundle.append(name);
        const destination = document.createElement("small");
        destination.textContent = row.bundle_target ? `Destined for ${row.bundle_target}` : "Destination unknown";
        bundle.append(document.createElement("br"), destination);
        if (row.bundle_pr_number) {
          const pr = document.createElement("a"); pr.href = row.bundle_pr_url; pr.textContent = `PR #${row.bundle_pr_number}`;
          pr.rel = "noreferrer"; pr.target = "_blank"; bundle.append(document.createElement("br"), pr);
        }
      } else bundle.textContent = "No bundle found";
      tr.append(bundle);

      const rebases = document.createElement("td");
      rebases.textContent = row.rebase_count === null ? "Unknown" : `${row.rebase_count} known`;
      rebases.title = "Count from this worktree's local HEAD reflog; reflog expiry can hide older rebases.";
      tr.append(rebases);

      const state = document.createElement("td");
      const stateLabel = document.createElement("span"); stateLabel.className = "task-state";
      stateLabel.textContent = row.task_state; state.append(stateLabel);
      if (row.dirty) { const note = document.createElement("small"); note.textContent = "Uncommitted changes present"; state.append(document.createElement("br"), note); }
      tr.append(state);
      body.append(tr);
    }
    if (!rows.length) {
      const tr = document.createElement("tr"), td = document.createElement("td");
      td.colSpan = 7; td.textContent = "No registered worktrees found."; tr.append(td); body.append(tr);
    }
    for (const button of document.querySelectorAll(".sort-button")) {
      const active = button.dataset.key === sortKey;
      const th = button.closest("th");
      th.setAttribute("aria-sort", active ? (sortDirection > 0 ? "ascending" : "descending") : "none");
      button.querySelector("span").textContent = active ? (sortDirection > 0 ? "▲" : "▼") : "";
    }
  }

  function candidateCard(candidate) {
    const card = document.createElement("article"); card.className = `cleaner-candidate${candidate.eligible ? "" : " is-blocked"}`;
    const title = document.createElement("strong"); title.textContent = candidate.branch;
    const reason = document.createElement("p"); reason.textContent = candidate.reason;
    const details = document.createElement("p"); details.className = "cleaner-path";
    const path = document.createElement("code"); path.textContent = candidate.path;
    details.append(path, document.createTextNode(` · HEAD ${candidate.head}`));
    card.append(title, reason, details);
    const proof = document.createElement("ul"); proof.className = "cleaner-proof";
    for (const item of candidate.proof || []) {
      const line = document.createElement("li");
      const mark = document.createElement("span"); mark.className = item.passed ? "proof-pass" : "proof-fail";
      mark.textContent = item.passed ? "PASS" : "FAIL";
      line.append(mark, document.createTextNode(` · ${item.label}: ${item.detail}`)); proof.append(line);
    }
    card.append(proof);
    if (candidate.eligible && selectionMode) {
      const label = document.createElement("label"); label.className = "cleaner-select";
      const tick = document.createElement("input"); tick.type = "checkbox"; tick.checked = selectedIds.has(candidate.id);
      tick.setAttribute("aria-label", `Select ${candidate.branch} for cleanup`);
      tick.addEventListener("change", () => {
        if (tick.checked) selectedIds.add(candidate.id); else selectedIds.delete(candidate.id);
        updateBatchControls();
      });
      label.append(tick, document.createTextNode("Select this worktree")); card.append(label);
    } else if (candidate.eligible) {
      const confirm = document.createElement("button"); confirm.type = "button"; confirm.textContent = "Confirm cleanup";
      confirm.addEventListener("click", () => cleanSelected([candidate.id], candidate.branch));
      card.append(confirm);
    }
    return card;
  }

  function updateBatchControls() {
    const toggle = byId("toggle-select"), run = byId("cleanup-selected");
    toggle.disabled = candidates.length === 0;
    toggle.textContent = selectionMode ? "Cancel selection" : "Select multiple";
    run.disabled = selectedIds.size === 0;
  }

  function showCleanupLog(entries) {
    const lines = (entries || []).map(entry =>
      `${entry.cleaned_at} · ${entry.branch} · ${entry.path} · HEAD ${entry.head}`
      + (entry.target ? ` · proof target ${entry.target}` : "")
      + (entry.proof || []).map(item => `\n  ${item.passed ? "PASS" : "FAIL"} · ${item.label}: ${item.detail}`).join("")
    );
    byId("cleanup-log-tail").textContent = lines.length ? lines.join("\n") : "No worktrees cleaned yet.";
  }

  async function cleanSelected(ids, label = "") {
    if (!ids.length) return;
    const prompt = `Remove ${ids.length} selected worktree${ids.length === 1 ? "" : "s"} and their local task branches? Each is rechecked before removal.`
      + (label ? `\n\n${label}` : "");
    if (!window.confirm(prompt)) return;
    byId("cleanup-selected").disabled = true;
    byId("cleaner-status").textContent = `Rechecking ${ids.length} selected worktree${ids.length === 1 ? "" : "s"}…`;
    try {
      const response = await fetch("/workbench/api/marshalls/worktree-cleaner/clean-selected", {
        method: "POST", credentials: "omit", cache: "no-store", redirect: "error",
        headers: {"Content-Type": "application/json"}, body: JSON.stringify({candidate_ids: ids}),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail || `Request failed (${response.status})`);
      const failed = result.results.filter(item => !item.removed);
      const unlogged = result.results.filter(item => item.removed && item.log_written === false);
      const message = `${result.removed_count} worktree${result.removed_count === 1 ? "" : "s"} cleaned.`
        + (failed.length ? ` ${failed.length} skipped: ${failed.map(item => item.message).join("; ")}` : "")
        + (unlogged.length ? ` ${unlogged.length} removed but could not be written to cleanup log.` : "");
      selectedIds.clear(); selectionMode = false;
      await load(); await scanCleaner(message);
    } catch (error) {
      byId("cleaner-status").textContent = `Cleanup stopped. ${error.message}`;
      updateBatchControls();
    }
  }

  async function scanCleaner(successMessage = "") {
    const button = byId("scan-cleaner"), status = byId("cleaner-status"), results = byId("cleaner-results");
    button.disabled = true; status.textContent = "Checking merge state, uncommitted files, and process working directories…";
    try {
      const response = await fetch("/workbench/api/marshalls/worktree-cleaner/preview", {
        credentials: "omit", cache: "no-store", redirect: "error",
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail || `Request failed (${response.status})`);
      byId("worktree-total").textContent = String(result.worktree_count);
      candidates = Array.isArray(result.candidates) ? result.candidates : [];
      selectedIds.clear();
      results.replaceChildren();
      for (const candidate of candidates) results.append(candidateCard({...candidate, eligible: true}));
      if (!candidates.length) {
        const empty = document.createElement("p"); empty.textContent = "No safe cleanup candidates found."; results.append(empty);
      }
      const blockedResults = byId("cleaner-blocked-results");
      blockedResults.replaceChildren();
      for (const candidate of result.blocked || []) blockedResults.append(candidateCard({...candidate, eligible: false}));
      showCleanupLog(result.cleanup_log);
      updateBatchControls();
      const countText = `${result.candidate_count} candidate${result.candidate_count === 1 ? "" : "s"}; ${result.blocked_count} worktree${result.blocked_count === 1 ? "" : "s"} protected or still active.`;
      status.textContent = successMessage ? `${successMessage} ${countText}` : countText;
    } catch (error) {
      status.textContent = `Could not scan worktrees. ${error.message}`;
      results.replaceChildren();
      byId("cleaner-blocked-results").replaceChildren();
    } finally { button.disabled = false; }
  }

  async function load() {
    const status = byId("inventory-status"), button = byId("refresh-worktrees");
    button.disabled = true; status.textContent = "Reading Git worktree and journal evidence…";
    try {
      const response = await fetch("/workbench/api/integrations/worktrees", {credentials: "omit", cache: "no-store", redirect: "error"});
      if (!response.ok) throw new Error(`Request failed (${response.status})`);
      const result = await response.json();
      rows = Array.isArray(result.worktrees) ? result.worktrees : [];
      byId("worktree-total").textContent = String(rows.length);
      const stamp = result.generated_at ? new Date(result.generated_at).toLocaleString() : "time unavailable";
      byId("inventory-meta").textContent = `${rows.length} registered worktrees · comparison base: ${result.base} · refreshed ${stamp}`;
      status.textContent = "Inventory loaded. Commit and rebase evidence is read-only.";
      render();
    } catch (error) {
      status.textContent = `Could not load local worktrees. ${error.message}`;
      byId("inventory-meta").textContent = "Inventory unavailable";
      rows = []; render();
    } finally { button.disabled = false; }
  }

  for (const button of document.querySelectorAll(".sort-button")) {
    button.addEventListener("click", () => {
      const key = button.dataset.key;
      if (sortKey === key) sortDirection *= -1;
      else { sortKey = key; sortDirection = 1; }
      render();
    });
  }
  byId("refresh-worktrees").addEventListener("click", load);
  byId("scan-cleaner").addEventListener("click", () => scanCleaner());
  byId("toggle-select").addEventListener("click", () => {
    selectionMode = !selectionMode; selectedIds.clear();
    const results = byId("cleaner-results"); results.replaceChildren(...candidates.map(candidate => candidateCard({...candidate, eligible: true})));
    updateBatchControls();
  });
  byId("cleanup-selected").addEventListener("click", () => cleanSelected([...selectedIds]));
  load();
  scanCleaner();
})();
