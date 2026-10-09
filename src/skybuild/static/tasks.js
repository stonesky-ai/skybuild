"use strict";

(() => {
  const byId = (id) => document.getElementById(id);
  const tabs = [...document.querySelectorAll("[data-tab]")];
  const formConnection = byId("connection-form");
  const formCreate = byId("create-form");
  const formDefer = byId("defer-form");
  const formResume = byId("resume-form");
  const previewMode = document.documentElement.dataset.skybuildPreview === "true";
  const fakeTimestamp = "2026-10-01T15:20:00Z";
  const fakeCommit = {
    fake: true, number: 1, sha: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", committed_at: "2026-10-01T15:00:00Z",
    author: "fake-author", box_name: "fake-box-01", review: {reviewer: "fake-reviewer", result: "passed", evidence_ref: "FAKE-REVIEW-REF"},
  };
  const fakeTask = {
    task_id: "FAKE-SKYBUILD-TASK-WORKBENCH", title: "Build the dedicated task workspace",
    description: "Example task record for previewing the Tasks page. This record and its journal are fabricated.",
    status: "in-progress", priority: 3, phase: "UI preview", next_action: "Review list, defer, and journal panels.",
    blocker: "", responsible: "fake-preview", assignee: "demo-box", dependencies: ["FAKE-SKYBUILD-BOOTSTRAP"],
    acceptance_criteria: ["Task list shows complete task fields", "Task journal expands commit and review evidence"],
    architecture_refs: ["FAKE-ARCH-REF"], revision: 3, created_at: "2026-10-01T14:10:00Z", updated_at: fakeTimestamp,
    metadata: {fake: true}, fake: true,
  };
  const fakeHistory = [
    {event_id: "FAKE-EVENT-001", task_id: fakeTask.task_id, actor: "fake-owner", operation: "created", revision: 1,
      created_at: "2026-10-01T14:10:00Z", reason: "Fake preview task created", after_state: {...fakeTask, revision: 1}, fake: true},
    {event_id: "FAKE-EVENT-002", task_id: fakeTask.task_id, actor: "fake-worker", operation: "updated", revision: 2,
      created_at: "2026-10-01T15:00:00Z", reason: "Fake implementation commit recorded", after_state: {...fakeTask, revision: 2},
      commits: [fakeCommit], fake: true},
    {event_id: "FAKE-EVENT-003", task_id: fakeTask.task_id, actor: "fake-reviewer", operation: "reviewed", revision: 3,
      created_at: fakeTimestamp, reason: "Fake independent review passed", after_state: {...fakeTask, revision: 3},
      review: {reviewer: "fake-reviewer", result: "passed", findings: 0}, fake: true},
  ];
  const fakeTasks = [fakeTask];

  let token = previewMode ? "local-preview" : "", project = previewMode ? "skybuild" : "", tasks = [], selectedTask = null, selectedHistory = [], cursor = null;
  let historyOffset = 0, historyHasMore = false, busy = false, epoch = 0;

  function notify(message, error = false) {
    byId("notice").textContent = message;
    byId("notice").classList.toggle("error", error);
  }

  function escapeText(value) {
    if (value === null || value === undefined || value === "") return "—";
    if (typeof value === "object") return JSON.stringify(value, null, 2);
    return String(value);
  }

  function switchTab(key) {
    for (const tab of tabs) {
      const active = tab.dataset.tab === key;
      tab.setAttribute("aria-selected", String(active));
      tab.tabIndex = active ? 0 : -1;
      byId(`panel-${tab.dataset.tab}`).hidden = !active;
    }
  }

  function controls() {
    const connected = Boolean(token);
    if (previewMode) {
      byId("preview-session").hidden = false;
      formConnection.hidden = true;
    }
    byId("project").disabled = connected || busy;
    byId("token").disabled = connected || busy;
    byId("connect").disabled = connected || busy;
    byId("logout").disabled = !connected || busy;
    byId("refresh-tasks").disabled = !connected || busy;
    byId("first-tasks").disabled = !connected || busy || cursor === null;
    byId("next-tasks").disabled = !connected || busy || tasks.length < 100 || tasks.some(task => task.fake);
    for (const field of formCreate.elements) field.disabled = !connected || busy || previewMode;
    const chosen = byId("defer-task").value;
    const task = tasks.find(item => item.task_id === chosen);
    const actionable = connected && !busy && task && !task.fake;
    for (const field of formDefer.elements) field.disabled = !actionable || task.status === "done";
    for (const field of formResume.elements) field.disabled = !actionable || task.status !== "deferred";
    byId("defer-state").textContent = !task ? "Select a task to review its deferral state."
      : task.fake ? "Fake preview task. Actions are disabled."
      : task.status === "deferred" ? "This task is deferred. Use Resume to undefer it into blocked reassessment."
      : `Current status: ${task.status}. Defer is available; Resume is available only while deferred.`;
    byId("refresh-history").disabled = !connected || busy || !selectedTask || selectedTask.fake;
    byId("load-more-history").disabled = !connected || busy || !selectedTask || selectedTask.fake || !historyHasMore;
    byId("defer-task").disabled = !connected || busy || tasks.length === 0;
  }

  async function request(suffix, options = {}) {
    if (previewMode) throw new Error("Local preview does not connect to the SkyBuild API.");
    const session = epoch;
    const headers = {Authorization: `Bearer ${token}`};
    if (options.body !== undefined) {
      headers["Content-Type"] = "application/json";
      headers["Idempotency-Key"] = crypto.randomUUID();
    }
    if (options.revision !== undefined) headers["If-Match"] = String(options.revision);
    const response = await fetch(`/api/v1/projects/${encodeURIComponent(project)}/${suffix}`, {
      method: options.method || "GET", headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      credentials: "omit", redirect: "error", cache: "no-store",
    });
    if (session !== epoch) throw new DOMException("Session ended", "AbortError");
    if (!response.ok) {
      const error = new Error(`Request failed (${response.status})`);
      error.status = response.status;
      throw error;
    }
    return response.json();
  }

  async function perform(operation) {
    if (busy) return;
    const session = epoch;
    busy = true;
    controls();
    try {
      await operation();
    } catch (error) {
      if (session !== epoch || error.name === "AbortError") return;
      if (error.status === 401) {
        disconnect();
        notify("Authentication failed. Connect with a valid token.", true);
      } else if (error.status === 403) notify("Access denied. Check project and task operation grants.", true);
      else if (error.status === 409) notify("Task changed since it was loaded. Refresh task and journal before retrying.", true);
      else if (error.status === 422) notify("Request was rejected. Check task fields, timezone, and deferral trigger.", true);
      else notify("Task service unavailable. Refresh before retrying.", true);
    } finally {
      if (session === epoch) {
        busy = false;
        controls();
      }
    }
  }

  function addCell(row, value) {
    const cell = document.createElement("td");
    cell.textContent = escapeText(value);
    row.append(cell);
    return cell;
  }

  function renderTasks() {
    const rows = byId("task-rows");
    rows.replaceChildren();
    tasks.forEach(task => {
      const row = document.createElement("tr");
      const name = document.createElement("td");
      const pick = document.createElement("button");
      pick.type = "button";
      pick.className = "task-name-button";
      pick.textContent = task.title;
      pick.addEventListener("click", () => perform(async () => selectTask(task.task_id, {openHistory: true})));
      if (selectedTask?.task_id === task.task_id) pick.setAttribute("aria-current", "true");
      if (task.fake) {
        const flag = document.createElement("span");
        flag.className = "fake-label";
        flag.textContent = "FAKE PREVIEW DATA";
        name.append(flag, document.createElement("br"));
      }
      name.append(pick);
      const id = document.createElement("span");
      id.className = "task-id";
      id.textContent = task.task_id;
      name.append(id);
      row.append(name);
      const status = addCell(row, task.status);
      const pill = document.createElement("span");
      pill.className = "status-pill";
      pill.textContent = escapeText(task.status);
      status.replaceChildren(pill);
      addCell(row, task.status === "deferred" ? "blocked on resume" : task.status === "done" ? "none (complete)" : "Not recorded by API");
      addCell(row, task.phase);
      addCell(row, task.priority);
      addCell(row, task.blocker || task.next_action);
      addCell(row, task.responsible);
      addCell(row, task.updated_at);
      const detailsCell = document.createElement("td");
      const details = document.createElement("details"), summary = document.createElement("summary"), record = document.createElement("pre");
      summary.textContent = task.fake ? "Expand fake record" : "Expand task record";
      record.textContent = JSON.stringify(task, null, 2);
      details.append(summary, record);
      detailsCell.append(details);
      row.append(detailsCell);
      if (selectedTask?.task_id === task.task_id) row.classList.add("selected-task-row");
      rows.append(row);
    });
    const fake = tasks.some(task => task.fake);
    byId("task-count").textContent = previewMode
      ? `${tasks.length} fake task${tasks.length === 1 ? "" : "s"} loaded · signed in as user1`
      : !token ? "Not connected"
        : `${tasks.length} task${tasks.length === 1 ? "" : "s"} loaded for ${project}${fake ? " · API returned no tasks; showing fake preview records" : ""}`;
    renderTaskPicker();
    controls();
  }

  function renderTaskPicker() {
    const picker = byId("defer-task");
    const keep = picker.value;
    picker.replaceChildren();
    if (!tasks.length) {
      const option = document.createElement("option");
      option.value = "";
      option.textContent = "No tasks loaded";
      picker.append(option);
    } else {
      for (const task of tasks) {
        const option = document.createElement("option");
        option.value = task.task_id;
        option.textContent = `${task.fake ? "[FAKE] " : ""}${task.task_id} · ${task.title} · ${task.status}`;
        picker.append(option);
      }
      picker.value = tasks.some(task => task.task_id === keep) ? keep : tasks[0].task_id;
    }
    controls();
  }

  async function loadTasks(afterTaskId = null) {
    if (previewMode) {
      const response = await fetch("/workbench/dev/tasks-preview.json", {credentials: "omit", cache: "no-store"});
      if (!response.ok) throw new Error("Local preview task list is unavailable.");
      const ledgerTasks = await response.json();
      cursor = null;
      tasks = [...fakeTasks, ...ledgerTasks];
      renderTasks();
      return;
    }
    const suffix = `tasks?limit=100&by_id=true${afterTaskId ? `&after_task_id=${encodeURIComponent(afterTaskId)}` : ""}`;
    const result = await request(suffix);
    cursor = afterTaskId;
    tasks = afterTaskId === null && result.length === 0 ? fakeTasks : result;
    renderTasks();
    if (selectedTask && !tasks.some(task => task.task_id === selectedTask.task_id)) {
      selectedTask = null;
      selectedHistory = [];
      renderTaskSummary(null);
      renderHistory();
    }
  }

  function renderTaskSummary(task) {
    const target = byId("selected-summary");
    target.replaceChildren();
    const heading = document.createElement("h3");
    heading.textContent = task ? `Task details · ${task.task_id}` : "Task details";
    target.append(heading);
    if (!task) {
      const empty = document.createElement("p");
      empty.textContent = "Select a task to view all fields.";
      target.append(empty);
      return;
    }
    if (task.fake) {
      const flag = document.createElement("span");
      flag.className = "fake-label";
      flag.textContent = "FAKE PREVIEW TASK · NO WRITES ENABLED";
      target.append(flag);
    }
    const info = document.createElement("dl");
    const fields = [
      ["Name", task.title], ["Description", task.description], ["Current status", task.status],
      ["Next status", task.status === "deferred" ? "blocked on resume" : "Not declared by API"],
      ["Next action", task.next_action], ["Phase", task.phase], ["Priority", task.priority],
      ["Blocker", task.blocker], ["Responsible", task.responsible], ["Assignee", task.assignee],
      ["Dependencies", task.dependencies], ["Acceptance criteria", task.acceptance_criteria],
      ["Architecture references", task.architecture_refs], ["Revision", task.revision],
      ["Created", task.created_at], ["Updated", task.updated_at],
    ];
    for (const [labelText, value] of fields) {
      const label = document.createElement("dt"), detail = document.createElement("dd");
      label.textContent = labelText;
      detail.textContent = escapeText(value);
      info.append(label, detail);
    }
    target.append(info);
    const full = document.createElement("details"), summary = document.createElement("summary"), record = document.createElement("pre");
    summary.textContent = "Full task record";
    record.textContent = JSON.stringify(task, null, 2);
    full.append(summary, record);
    target.append(full);
  }

  function nestedCommitRecords(value, found = [], depth = 0) {
    if (!value || typeof value !== "object" || depth > 8) return found;
    if (Array.isArray(value)) {
      for (const child of value) nestedCommitRecords(child, found, depth + 1);
      return found;
    }
    const sha = value.sha || value.commit_id || value.hash || (typeof value.commit === "string" ? value.commit : null);
    if (sha) found.push(value);
    for (const [key, child] of Object.entries(value)) {
      if (typeof child === "string" && /^(?:source_head|candidate_commit|base_commit|target_commit|commit_sha|commit_id|sha|hash)$/.test(key)) {
        found.push({label: key.replaceAll("_", " "), sha: child, target_ref: value.target_ref, fake: value.fake});
      } else nestedCommitRecords(child, found, depth + 1);
    }
    return found;
  }

  function commitRecords(event) {
    const after = event.after_state || {};
    const metadata = after.metadata || {};
    const commits = nestedCommitRecords(event);
    const seen = new Set();
    return commits.filter(commit => {
      const sha = commit.sha || commit.commit || commit.commit_id || commit.hash;
      if (!sha || seen.has(String(sha))) return false;
      seen.add(String(sha));
      return true;
    });
  }

  function kvList(entries, parent, className = "commit-fields") {
    const list = document.createElement("dl");
    list.className = className;
    for (const [key, value] of entries) {
      if (value === undefined || value === null || value === "") continue;
      const term = document.createElement("dt"), detail = document.createElement("dd");
      term.textContent = key;
      detail.textContent = escapeText(value);
      list.append(term, detail);
    }
    parent.append(list);
  }

  function findField(value, keys, depth = 0) {
    if (!value || typeof value !== "object" || depth > 8) return undefined;
    for (const [key, child] of Object.entries(value)) {
      if (keys.includes(key.toLowerCase()) && (typeof child === "string" || typeof child === "number")) return child;
    }
    for (const child of Object.values(value)) {
      const found = findField(child, keys, depth + 1);
      if (found !== undefined) return found;
    }
    return undefined;
  }

  function renderEvidence(event, parent) {
    const commits = commitRecords(event);
    const after = event.after_state || {};
    const metadata = after.metadata || {};
    const completion = metadata._skybuild_completion;
    const review = event.review || event.reviews || (completion && completion.review);
    const box = event.box_name || event.boxname || event.box || metadata.box_name || metadata.boxname || metadata.box || metadata.worker_box
      || findField(metadata, ["box_name", "boxname", "box"]);
    const block = document.createElement("details");
    block.className = "journal-extra";
    const label = document.createElement("summary");
    label.textContent = `Commit and review evidence (${commits.length} commit${commits.length === 1 ? "" : "s"})`;
    block.append(label);
    if (!commits.length) {
      const note = document.createElement("p");
      note.textContent = "No commit records are attached to this journal event.";
      block.append(note);
    }
    for (const commit of commits) {
      const fold = document.createElement("details");
      fold.className = "commit-record";
      const head = document.createElement("summary");
      const sha = commit.sha || commit.commit || commit.commit_id || commit.hash;
      head.textContent = `${commit.fake ? "[FAKE] " : ""}${commit.label || commit.number ? `${commit.label || "Commit"} ${commit.number || ""}` : "Commit"} · ${sha}`;
      fold.append(head);
      kvList([
        ["Commit ID", sha], ["Commit number", commit.number || commit.commit_number],
        ["Commit datetime", commit.committed_at || commit.commit_time || commit.commit_datetime || commit.timestamp || commit.datetime || commit.created_at],
        ["Author", commit.author || commit.committer || commit.who || completion?.author], ["Recorded by", event.actor],
        ["Box", commit.box_name || commit.boxname || commit.box || box],
        ["Target ref", commit.target_ref], ["Journal recorded at", event.created_at],
      ], fold);
      if (commit.review || review) {
        const reviewDetails = document.createElement("details");
        const reviewSummary = document.createElement("summary");
        const reviewBody = document.createElement("pre");
        reviewSummary.textContent = "Review";
        reviewBody.textContent = JSON.stringify(commit.review || review, null, 2);
        reviewDetails.append(reviewSummary, reviewBody);
        fold.append(reviewDetails);
      }
      block.append(fold);
    }
    if (review && !commits.length) {
      const reviewDetails = document.createElement("details");
      const summary = document.createElement("summary"), body = document.createElement("pre");
      summary.textContent = "Review record";
      body.textContent = JSON.stringify(review, null, 2);
      reviewDetails.append(summary, body);
      block.append(reviewDetails);
    }
    if (event.fake) {
      const marker = document.createElement("p");
      marker.className = "fake-label";
      marker.textContent = "FAKE JOURNAL DATA";
      block.append(marker);
    }
    parent.append(block);
  }

  function renderHistory() {
    byId("history-task").textContent = selectedTask
      ? `${selectedTask.fake ? "[FAKE PREVIEW] " : ""}${selectedTask.task_id} · ${selectedTask.title}`
      : "Choose a task from Task List.";
    const list = byId("history-list");
    list.replaceChildren();
    for (const event of selectedHistory) {
      const item = document.createElement("li");
      if (event.fake) {
        const flag = document.createElement("span");
        flag.className = "fake-label";
        flag.textContent = "FAKE JOURNAL DATA";
        item.append(flag, document.createElement("br"));
      }
      const summary = document.createElement("p");
      summary.className = "journal-summary";
      summary.textContent = `Revision ${event.revision} · ${event.created_at || "time not recorded"} · ${event.actor || "actor not recorded"} · ${event.operation || "event"}`;
      item.append(summary);
      const reason = document.createElement("p");
      reason.className = "journal-reason";
      reason.textContent = event.reason || "No reason recorded.";
      item.append(reason);
      renderEvidence(event, item);
      const full = document.createElement("details"), fullLabel = document.createElement("summary"), body = document.createElement("pre");
      full.className = "journal-extra";
      fullLabel.textContent = "Expand full journal event";
      body.textContent = JSON.stringify(event, null, 2);
      full.append(fullLabel, body);
      item.append(full);
      list.append(item);
    }
    if (selectedTask && selectedHistory.length === 0) {
      const item = document.createElement("li");
      item.textContent = "No journal entries returned for this task.";
      list.append(item);
    }
    controls();
  }

  async function selectTask(taskId, {openHistory = false} = {}) {
    let task, history;
    const listed = tasks.find(item => item.task_id === taskId);
    if (listed?.fake) {
      task = listed;
      history = task.task_id === fakeTask.task_id
        ? fakeHistory.map(event => ({...event, after_state: {...event.after_state, ...task}}))
        : [{event_id: `FAKE-${task.task_id}-SNAPSHOT`, task_id: task.task_id, actor: "fake-preview",
          operation: "previewed", revision: task.revision, created_at: "mastertodo.md snapshot",
          reason: "FAKE: Read-only task record parsed from mastertodo.md. This is not API journal history.",
          after_state: task, fake: true}];
    } else {
      task = await request(`tasks/${encodeURIComponent(taskId)}`);
      history = await request(`tasks/${encodeURIComponent(taskId)}/history?limit=100&offset=0`);
    }
    selectedTask = task;
    selectedHistory = history;
    historyOffset = history.length;
    historyHasMore = !task.fake && history.length === 100;
    renderTaskSummary(task);
    renderHistory();
    renderTasks();
    if (openHistory) switchTab("history");
  }

  async function refreshSelected() {
    if (!selectedTask || selectedTask.fake) return;
    await selectTask(selectedTask.task_id);
  }

  function disconnect() {
    epoch += 1;
    token = "";
    project = "";
    tasks = [];
    selectedTask = null;
    selectedHistory = [];
    cursor = null;
    historyOffset = 0;
    historyHasMore = false;
    byId("project").value = "";
    byId("token").value = "";
    formCreate.reset();
    formDefer.reset();
    formResume.reset();
    renderTasks();
    renderTaskSummary(null);
    renderHistory();
    controls();
  }

  for (const tab of tabs) tab.addEventListener("click", () => switchTab(tab.dataset.tab));
  if (previewMode) {
    void loadTasks().then(() => selectTask(fakeTask.task_id)).then(() => {
      notify("Local fake session active as user1. mastertodo queue shown as fake, read-only data.");
    }).catch(() => notify("Local preview task list is unavailable. No API or database connection was made.", true));
  }
  switchTab("list");
  formConnection.addEventListener("submit", event => {
    event.preventDefault();
    token = byId("token").value;
    project = byId("project").value.trim();
    byId("token").value = "";
    perform(async () => {
      await loadTasks();
      notify(`Connected to ${project}.`);
      if (tasks.some(task => task.fake)) await selectTask(fakeTask.task_id);
    });
  });
  byId("logout").addEventListener("click", () => { disconnect(); notify("Logged out. Token and task data cleared."); });
  byId("refresh-tasks").addEventListener("click", () => perform(async () => { await loadTasks(); notify("Task list refreshed."); }));
  byId("first-tasks").addEventListener("click", () => perform(async () => { await loadTasks(); notify("First task page loaded."); }));
  byId("next-tasks").addEventListener("click", () => perform(async () => {
    const last = tasks[tasks.length - 1];
    if (last && !last.fake) await loadTasks(last.task_id);
  }));
  byId("refresh-history").addEventListener("click", () => perform(async () => { await refreshSelected(); notify("Task journal refreshed."); }));
  byId("load-more-history").addEventListener("click", () => perform(async () => {
    const older = await request(`tasks/${encodeURIComponent(selectedTask.task_id)}/history?limit=100&offset=${historyOffset}`);
    selectedHistory.push(...older);
    historyOffset += older.length;
    historyHasMore = older.length === 100;
    renderHistory();
  }));
  byId("defer-task").addEventListener("change", controls);

  formCreate.addEventListener("submit", event => {
    event.preventDefault();
    perform(async () => {
      const body = {
        task_id: byId("new-id").value.trim(), title: byId("new-title").value.trim(),
        description: byId("new-description").value, priority: Number(byId("new-priority").value),
        responsible: byId("new-responsible").value.trim() || "lead",
        next_action: byId("new-next").value.trim() || "Triage this task",
      };
      const created = await request("tasks", {method: "POST", body});
      formCreate.reset();
      await loadTasks();
      await selectTask(created.task_id);
      switchTab("list");
      notify(`Task ${created.task_id} created.`);
    });
  });

  formDefer.addEventListener("submit", event => {
    event.preventDefault();
    const task = tasks.find(item => item.task_id === byId("defer-task").value);
    if (!task || task.fake) return;
    const until = byId("defer-until").value.trim();
    const milestone = byId("defer-milestone").value.trim();
    if (Boolean(until) === Boolean(milestone)) {
      notify("Enter exactly one deferral trigger: a timezone-aware time or milestone task ID.", true);
      return;
    }
    perform(async () => {
      const body = {reason: byId("defer-reason").value.trim()};
      if (until) body.until = until;
      if (milestone) body.milestone_task_id = milestone;
      await request(`tasks/${encodeURIComponent(task.task_id)}/actions/defer`, {method: "POST", body, revision: task.revision});
      formDefer.reset();
      await loadTasks();
      byId("defer-task").value = task.task_id;
      controls();
      notify(`Task ${task.task_id} deferred.`);
    });
  });

  formResume.addEventListener("submit", event => {
    event.preventDefault();
    const task = tasks.find(item => item.task_id === byId("defer-task").value);
    if (!task || task.fake || task.status !== "deferred") return;
    perform(async () => {
      await request(`tasks/${encodeURIComponent(task.task_id)}/actions/resume`, {
        method: "POST", body: {reason: byId("resume-reason").value.trim()}, revision: task.revision,
      });
      formResume.reset();
      await loadTasks();
      byId("defer-task").value = task.task_id;
      controls();
      notify(`Task ${task.task_id} resumed for reassessment.`);
    });
  });

  if (typeof window !== "undefined" && typeof window.setInterval === "function") {
    window.setInterval(async () => {
      if (!token || busy) return;
      const session = epoch;
      try { await loadTasks(); } catch (error) {
        if (session === epoch && error.name !== "AbortError") byId("task-count").textContent = "Task refresh failed; retrying automatically.";
      }
    }, 15000);
  }

  renderTasks();
  renderTaskSummary(null);
  renderHistory();
  controls();
})();
