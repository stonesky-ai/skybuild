"use strict";

(() => {
  const byId = (id) => document.getElementById(id);
  const connection = byId("connection-form");
  const create = byId("create-form");
  const edit = byId("edit-form");
  const action = byId("action-form");
  const structure = byId("structure-form");
  const workflowForm = byId("workflow-form");
  let workflowView = null, pendingWorkflow = null;
  const controlNames = {claim: "Claim work", hold: "Hold", release_hold: "Release hold", defer: "Defer", resume_deferred: "Resume deferred task", reopen: "Reopen", update_control: "Change hold or deferral details"};
  let token = "", project = "", selected = null, busy = false, stale = false, epoch = 0, controller = null, reconcileCursor = null, structuralPlan = null, historyOffset = 0, historyHasMore = false, taskCursor = null, taskHasMore = false;

  let boardOffset = 0, boardNext = null;
  const places = ["ready", "working", "validating", "integrating", "done", "deferred", "hold"];

  class ApiError extends Error {
    constructor(status) { super(`Request failed (${status})`); this.status = status; }
  }

  function notice(message, error = false) {
    byId("notice").textContent = message;
    byId("notice").classList.toggle("error", error);
  }

  function controls() {
    const connected = Boolean(token);
    const unresolved = Boolean(pendingWorkflow?.unknown);
    byId("project").disabled = connected || busy;
    byId("token").disabled = connected || busy;
    byId("connect").disabled = connected || busy;
    byId("logout").disabled = !connected;
    byId("refresh-board").disabled = !connected || busy;
    byId("first-board").disabled = !connected || busy || boardOffset === 0;
    byId("next-board").disabled = !connected || busy || boardNext === null;
    for (const button of byId("workflow-board").querySelectorAll("button")) button.disabled = busy || !connected;
    byId("refresh-tasks").disabled = !connected || busy;
    byId("first-tasks").disabled = !connected || busy || taskCursor === null;
    byId("next-tasks").disabled = !connected || busy || !taskHasMore;
    byId("reconcile-due").disabled = !connected || busy || unresolved;
    byId("refresh-selected").disabled = !connected || busy || !selected;
    byId("load-more-history").disabled = !connected || busy || !selected || !historyHasMore;
    for (const field of create.elements) field.disabled = !connected || busy || unresolved;
    for (const field of edit.elements) field.disabled = !connected || busy || !selected;
    for (const id of ["edit-status", "edit-phase", "edit-blocker"]) byId(id).disabled = true;
    byId("save").disabled = !connected || busy || !selected || stale || unresolved;
    for (const field of action.elements) field.disabled = !connected || busy || !selected || stale || unresolved;
    for (const field of workflowForm.elements) field.disabled = !connected || busy || !selected || stale || unresolved || !workflowView;
    byId("workflow-submit").disabled = !connected || busy || !selected || stale || unresolved || !workflowView || !(workflowView.available_actions || []).some(name => controlNames[name]);
    byId("workflow-retry").disabled = !connected || busy || !unresolved;
    for (const button of byId("workflow-dependencies").querySelectorAll("button")) button.disabled = !connected || busy;
    for (const field of structure.elements) field.disabled = !connected || busy || !selected || stale || unresolved;
    byId("apply-structure").disabled = !connected || busy || !selected || stale || unresolved || !structuralPlan;
    for (const button of byId("task-list").querySelectorAll("button")) button.disabled = busy || !connected;
  }

  function disconnect() {
    epoch += 1;
    if (controller) controller.abort();
    token = ""; project = ""; selected = null; stale = false; busy = false; reconcileCursor = null; structuralPlan = null; historyOffset = 0; historyHasMore = false; taskCursor = null; taskHasMore = false;
    byId("project").value = "";
    byId("token").value = "";
    create.reset(); edit.reset(); action.reset(); structure.reset();
    byId("structure-preview").textContent = "No plan previewed.";
    byId("task-list").replaceChildren(); byId("history").replaceChildren();
    byId("lineage").replaceChildren();
    byId("task-count").textContent = "Not connected";
    boardOffset = 0; boardNext = null;
    byId("workflow-board").replaceChildren();
    byId("board-summary").textContent = "Not connected";
    byId("selection").textContent = "Select a task to view its definition and history.";
    byId("full-task-record").textContent = "No task selected.";
    workflowView = null; pendingWorkflow = null; clearWorkflow();
    controls();
  }

  async function request(suffix, options = {}) {
    const session = epoch;
    const activeController = new AbortController();
    controller = activeController;
    const headers = { Authorization: `Bearer ${token}` };
    if (options.body !== undefined) {
      headers["Content-Type"] = "application/json";
      headers["Idempotency-Key"] = options.operationKey || crypto.randomUUID();
    }
    if (options.revision !== undefined) headers["If-Match"] = String(options.revision);
    const timeout = setTimeout(() => activeController.abort(), 15000);
    try {
      const response = await fetch(`/api/v1/projects/${encodeURIComponent(project)}/${suffix}`, {
        method: options.method || "GET", headers,
        body: options.body === undefined ? undefined : JSON.stringify(options.body),
        signal: activeController.signal, credentials: "omit", redirect: "error", cache: "no-store",
      });
      if (session !== epoch) throw new DOMException("Session ended", "AbortError");
      if (!response.ok) throw new ApiError(response.status);
      const result = await response.json();
      if (session !== epoch) throw new DOMException("Session ended", "AbortError");
      return result;
    } finally {
      clearTimeout(timeout);
    }
  }

  async function perform(operation, mutation = false, replay = false) {
    if (busy || (mutation && pendingWorkflow?.unknown && !replay)) return;
    const session = epoch;
    busy = true; controls(); notice("Working…");
    try {
      await operation();
    } catch (error) {
      if (session !== epoch) return;
      if (error.status === 401) {
        disconnect(); notice("Authentication failed (401). Connect with a valid token.", true);
      } else if (error.status === 403) {
        notice("Access denied (403). Check the project and operation scopes.", true);
      } else if (error.status === 409) {
        if (mutation && selected) stale = true;
        notice("Conflict (409). Refresh the selected task or task list before trying again. No changes were overwritten.", true);
      } else if (error.status === 422 || error.status === 413) {
        notice("Invalid request. Check required fields, IDs, dependency count, and text lengths.", true);
      } else {
        notice("Service unavailable or request failed. Refresh to inspect current state before retrying a mutation.", true);
      }
    } finally {
      if (session === epoch) { busy = false; controller = null; controls(); }
    }
  }

  async function loadTasks(afterTaskId = null) {
    const tasks = await request(`tasks?limit=100&by_id=true${afterTaskId === null ? "" : `&after_task_id=${encodeURIComponent(afterTaskId)}`}`);
    if (afterTaskId !== null && tasks.length === 0) {
      taskHasMore = false;
      return false;
    }
    const list = byId("task-list"); list.replaceChildren();
    for (const task of tasks) {
      const item = document.createElement("li"), button = document.createElement("button");
      button.type = "button";
      button.dataset.taskId = task.task_id;
      button.textContent = `${task.task_id}: ${task.title} (${task.status} · ${task.phase})\n${task.blocker || task.next_action || "No next action"} · ${task.responsible}`;
      button.setAttribute("aria-current", String(selected?.task_id === task.task_id));
      button.addEventListener("click", () => perform(async () => { await selectTask(task.task_id); notice("Task loaded."); }));
      item.append(button); list.append(item);
    }
    taskCursor = afterTaskId;
    taskHasMore = tasks.length === 100;
    byId("task-count").textContent = `${tasks.length} tasks shown for ${project}${afterTaskId === null ? "" : ` after ${afterTaskId}`}`;
    return true;
  }

  async function loadBoard(offset = 0) {
    const board = await request(`workflow-board?limit=100&offset=${offset}`);
    const root = byId("workflow-board"); root.replaceChildren();
    for (const place of places) {
      const column = board.columns.find(item => item.place === place);
      const section = document.createElement("section"), heading = document.createElement("h3");
      section.className = "workflow-column";
      heading.textContent = `${place[0].toUpperCase() + place.slice(1)} (${column.count})`;
      const age = document.createElement("p");
      age.className = "hint";
      age.textContent = column.oldest_age_seconds === null ? "Oldest age unknown" : `Oldest: ${Math.floor(column.oldest_age_seconds / 60)} minutes`;
      if (column.unknown_age_count) age.textContent += ` · ${column.unknown_age_count} unknown age(s)`;
      const list = document.createElement("ul"); list.className = "task-list";
      for (const task of board.tasks.filter(item => item.place === place)) {
        const item = document.createElement("li"), button = document.createElement("button");
        button.type = "button"; button.dataset.taskId = task.task_id;
        const stages = ["unit_tests", "scans", "long_tests", "code_review", "needs_rebase"].map(stage => {
          const results = (task.validation || []).filter(result => result.stage === stage);
          return `${stage}: ${results.length ? results.map(result => result.state).join(", ") : "unavailable"}`;
        }).join(" · ");
        button.textContent = `${task.task_id}: ${task.title}\nPriority ${task.priority} · ${task.responsible}\n${task.blocker || task.next_action || "No next action"}`;
        if (place === "hold") button.textContent += `\nHold reason: ${task.hold_reason || task.blocker || "Unavailable"}. Release: explicit release after active effects are resolved.`;
        if (place === "deferred") button.textContent += `\nDeferred reason: ${task.hold_reason || task.blocker || "Unavailable"}. Resume condition: ${task.deferred_until ? `date ${task.deferred_until}` : task.milestone_task_id ? `milestone ${task.milestone_task_id}` : "explicit owner decision"}.`;
        if (task.blocked_dependencies.length) button.textContent += `\nWaiting for: ${task.blocked_dependencies.join(", ")}`;
        button.textContent += `\nEvidence: ${task.evidence_freshness || "unavailable"}\n${stages}`;
        button.setAttribute("aria-current", String(selected?.task_id === task.task_id));
        button.addEventListener("click", () => perform(async () => { await selectTask(task.task_id); notice("Task loaded."); }));
        item.append(button); list.append(item);
      }
      section.append(heading, age, list); root.append(section);
    }
    boardOffset = offset; boardNext = board.next_offset;
    byId("board-summary").textContent = `${board.total} workflow tasks for ${project}. ${board.tasks.length} cards shown. Ready: ${board.ready_dependencies_complete} with current dependency acceptance, ${board.ready_dependencies_blocked} waiting for dependencies. ${board.unenrolled_count} task(s) await workflow enrollment.`;
  }

  byId("refresh-board").addEventListener("click", () => perform(async () => { await loadBoard(); notice("Workflow refreshed."); }));
  byId("first-board").addEventListener("click", () => perform(async () => { await loadBoard(); notice("First workflow page loaded."); }));
  byId("next-board").addEventListener("click", () => perform(async () => { if (boardNext !== null) await loadBoard(boardNext); notice("Workflow page loaded."); }));
  byId("board-view").addEventListener("change", () => {
    byId("workflow-board").classList.toggle("list-view", byId("board-view").value === "list");
  });

  function clearWorkflow() {
    for (const id of ["workflow-requirements", "workflow-dependencies", "workflow-evidence", "workflow-findings", "workflow-disabled", "workflow-event", "workflow-path", "workflow-transitions"]) byId(id).replaceChildren();
    workflowForm.reset();
    byId("workflow-state").textContent = "No task selected.";
    byId("workflow-condition").textContent = "";
    showPendingWorkflow();
  }

  function showPendingWorkflow() {
    byId("workflow-operation").textContent = pendingWorkflow ? `Operation ${pendingWorkflow.key} for ${pendingWorkflow.taskId} at revision ${pendingWorkflow.revision}.${pendingWorkflow.unknown ? " Outcome unknown. Refresh can inspect state. Retry unchanged operation to retrieve the original outcome." : ""}` : "No pending operation.";
  }

  async function sendWorkflowOperation(operation) {
    try {
      await request(operation.path, {method: "POST", body: operation.body, revision: operation.revision, operationKey: operation.key});
      pendingWorkflow = null;
      await loadTasks(); await loadBoard(); await selectTask(operation.taskId);
      notice("Workflow operation confirmed. Current state loaded.");
    } catch (error) {
      if (pendingWorkflow === operation) {
        // A client rejection is definitive. Transport and server failures can follow a commit.
        if (error.status && error.status < 500) pendingWorkflow = null;
        else operation.unknown = true;
      }
      stale = true; showPendingWorkflow(); throw error;
    }
  }

  byId("workflow-retry").addEventListener("click", () => {
    if (!pendingWorkflow?.unknown || pendingWorkflow.project !== project) return;
    const operation = pendingWorkflow;
    perform(() => sendWorkflowOperation(operation), true, true);
  });

  function textItem(listId, value) {
    const item = document.createElement("li"); item.textContent = value; byId(listId).append(item); return item;
  }

  function referenceItem(value) {
    const item = document.createElement("li");
    try {
      const url = new URL(value);
      if (!["https:", "http:"].includes(url.protocol)) throw new Error();
      const link = document.createElement("a"); link.href = url.href; link.textContent = value;
      link.rel = "noreferrer noopener"; item.append(link);
    } catch { item.textContent = value; }
    byId("workflow-evidence").append(item);
  }

  function renderWorkflow() {
    clearWorkflow();
    if (!workflowView) { byId("workflow-state").textContent = "This task awaits workflow enrollment."; return; }
    const token = workflowView.token;
    const placeLabel = name => name ? name[0].toUpperCase() + name.slice(1) : "Same place";
    const transitions = workflowView.transitions || [];
    const normal = ["claim", "submit", "freeze", "accept"].map(event => transitions.find(spec => spec.event === event)).filter(Boolean);
    if (normal.length) {
      textItem("workflow-path", placeLabel(normal[0].sources[0]));
      for (const spec of normal) textItem("workflow-path", placeLabel(spec.destination));
    }
    for (const spec of transitions) textItem("workflow-transitions", `${spec.event.replaceAll("_", " ")}: ${spec.sources.map(placeLabel).join(", ")} to ${placeLabel(spec.destination)}`);

    byId("workflow-state").textContent = `${token.place[0].toUpperCase() + token.place.slice(1)} · Revision ${selected.revision} · Evidence: ${selected.evidence_freshness || "unavailable"}${token.pending_action ? ` · Pending: ${token.pending_action}` : ""}`;
    if (token.place === "hold") byId("workflow-condition").textContent = `Hold reason: ${token.hold_reason || selected.blocker || "Unavailable"}. Release: explicit release after active effects are resolved.`;
    if (token.place === "deferred") byId("workflow-condition").textContent = `Deferred reason: ${token.hold_reason || selected.blocker || "Unavailable"}. Resume condition: ${token.deferred_until ? `date ${token.deferred_until}` : token.milestone_task_id ? `milestone ${token.milestone_task_id}` : "explicit owner decision"}.`;
    for (const requirement of [...new Set([...(selected.acceptance_criteria || []), ...(token.requirements || [])])]) textItem("workflow-requirements", requirement);
    for (const dependency of selected.dependencies || []) {
      const item = document.createElement("li"), button = document.createElement("button");
      button.type = "button"; button.textContent = dependency;
      button.addEventListener("click", () => perform(async () => { await selectTask(dependency); notice("Dependency loaded."); }));
      item.append(button); byId("workflow-dependencies").append(item);
    }
    if (token.milestone_task_id) {
      const item = document.createElement("li"), button = document.createElement("button");
      button.type = "button"; button.textContent = `Milestone: ${token.milestone_task_id}`;
      button.addEventListener("click", () => perform(async () => { await selectTask(token.milestone_task_id); notice("Milestone loaded."); }));
      item.append(button); byId("workflow-dependencies").append(item);
    }
    for (const result of token.evidence || []) {
      textItem("workflow-evidence", `${result.stage}: ${result.state} · ${result.producer || "Unknown producer"} · Source ${result.source_head || "unavailable"} · Base ${result.target_base || "unavailable"}`);
      for (const artifact of result.artifacts || []) referenceItem(artifact);
      for (const finding of result.findings || []) textItem("workflow-findings", finding);
    }
    for (const link of token.links || []) referenceItem(link);
    for (const finding of [...(token.findings || []), ...(token.faults || [])]) textItem("workflow-findings", finding);
    const available = new Set(workflowView.available_actions || []);
    for (const [name, label] of Object.entries(controlNames)) {
      const option = document.createElement("option"); option.value = name; option.textContent = label; option.disabled = !available.has(name);
      byId("workflow-event").append(option);
      if (!available.has(name)) textItem("workflow-disabled", `${label}: ${workflowView.disabled_actions?.[name] || "Required permission or state is unavailable"}`);
    }
    byId("workflow-event").value = Object.keys(controlNames).find(name => available.has(name)) || "";
    for (const name of available) if (!controlNames[name]) textItem("workflow-disabled", `${name.replaceAll("_", " ")}: The responsible component records this transition.`);
    for (const [name, reason] of Object.entries(workflowView.disabled_actions || {})) if (!controlNames[name]) textItem("workflow-disabled", `${name.replaceAll("_", " ")}: ${reason}`);
  }

  workflowForm.addEventListener("submit", (event) => {
    event.preventDefault();
    if (!selected || stale || !workflowView || pendingWorkflow?.unknown) return;
    const name = byId("workflow-event").value;
    if (!controlNames[name] || !(workflowView.available_actions || []).includes(name)) return;
    const body = name === "claim" ? {lease_seconds: 300} : {event: name, reason: byId("workflow-reason").value};
    if (name !== "claim") {
      if (!body.reason.trim()) { notice("A workflow reason is required.", true); return; }
      if (byId("workflow-next").value) body.next_action = byId("workflow-next").value;
      if (name === "defer" || name === "update_control") {
        if (byId("workflow-until").value) body.until = byId("workflow-until").value;
        if (byId("workflow-milestone").value) body.milestone_task_id = byId("workflow-milestone").value;
      }
    }
    const taskId = selected.task_id, revision = selected.revision;
    const signature = JSON.stringify({taskId, revision, body});
    pendingWorkflow = {signature, key: crypto.randomUUID(), taskId, revision, project,
      body: JSON.parse(JSON.stringify(body)), path: `tasks/${encodeURIComponent(taskId)}/${name === "claim" ? "claim" : "workflow"}`, unknown: false};
    showPendingWorkflow();
    const operation = pendingWorkflow;
    perform(() => sendWorkflowOperation(operation), true);
  });

  function appendHistory(history) {
    const events = byId("history");
    for (const event of history) {
      const item = document.createElement("li"), summary = document.createElement("p");
      summary.textContent = `Revision ${event.revision} · ${event.actor} · ${event.operation} · ${event.created_at || event.at || ""} · ${event.reason || ""}`;
      const details = document.createElement("details"), label = document.createElement("summary"), body = document.createElement("pre");
      label.textContent = "Event details"; body.textContent = JSON.stringify(event, null, 2);
      details.append(label, body); item.append(summary, details); events.append(item);
    }
    historyOffset += history.length;
    historyHasMore = history.length === 100;
  }

  async function selectTask(taskId) {
    const path = `tasks/${encodeURIComponent(taskId)}`;
    let task = await request(path);
    const workflow = task.place ? await request(`${path}/workflow`) : null;
    if (workflow) task = workflow.task;
    const history = await request(`${path}/history?limit=100&offset=0`);
    const lineage = await request(`${path}/lineage`);
    selected = task; stale = false; workflowView = workflow; renderWorkflow();
    byId("full-task-record").textContent = JSON.stringify(task, null, 2);
    structuralPlan = null; byId("structure-preview").textContent = "No plan previewed.";
    byId("selection").textContent = `${task.task_id} · Revision ${task.revision}`;
    for (const [id, field] of [
      ["edit-title", "title"], ["edit-description", "description"], ["edit-status", "status"],
      ["edit-phase", "phase"], ["edit-next", "next_action"], ["edit-blocker", "blocker"], ["edit-responsible", "responsible"],
    ]) byId(id).value = task[field] ?? "";
    byId("edit-dependencies").value = task.dependencies.join("\n");
    byId("edit-acceptance").value = JSON.stringify(task.acceptance_criteria, null, 2);
    byId("edit-architecture").value = JSON.stringify(task.architecture_refs, null, 2);
    byId("history").replaceChildren(); historyOffset = 0; appendHistory(history);
    const links = byId("lineage"); links.replaceChildren();
    if (!lineage.length) {
      const item = document.createElement("li"); item.textContent = "No split or merge lineage."; links.append(item);
    }
    for (const edge of lineage) {
      const item = document.createElement("li");
      item.textContent = `${edge.source_task_id} → ${edge.target_task_id} (${edge.action})`;
      links.append(item);
    }
    for (const button of [...byId("task-list").querySelectorAll("button"), ...byId("workflow-board").querySelectorAll("button")]) {
      button.setAttribute("aria-current", String(button.dataset.taskId === task.task_id));
    }
  }

  connection.addEventListener("submit", (event) => {
    event.preventDefault();
    token = byId("token").value; project = byId("project").value;
    byId("token").value = "";
    perform(async () => { await loadTasks(); await loadBoard(); notice(`Connected to ${project}.`); });
  });
  byId("logout").addEventListener("click", () => { disconnect(); notice("Logged out. Private task data and token cleared."); });
  byId("refresh-tasks").addEventListener("click", () => perform(async () => { await loadTasks(); notice("Task list refreshed."); }));
  byId("first-tasks").addEventListener("click", () => perform(async () => { await loadTasks(); notice("First task page loaded."); }));
  byId("next-tasks").addEventListener("click", () => perform(async () => {
    if (!taskHasMore) return;
    const buttons = byId("task-list").querySelectorAll("button");
    if (!buttons.length) return;
    const advanced = await loadTasks(buttons[buttons.length - 1].dataset.taskId);
    notice(advanced ? "Next task page loaded." : "No more tasks.");
  }));
  byId("reconcile-due").addEventListener("click", () => perform(async () => {
    let total = 0, pages = 0;
    do {
      const result = await request(`tasks/reconcile-due?limit=100${reconcileCursor === null ? "" : `&after_task_id=${encodeURIComponent(reconcileCursor)}`}`, {method: "POST", body: {}});
      total += result.reassessed.length;
      reconcileCursor = result.next_after_task_id;
      pages += 1;
      if (reconcileCursor === null) break;
    } while (pages < 20);
    await loadTasks(); await loadBoard(); notice(`${total} due task(s) sent for reassessment.${reconcileCursor ? " Continue scan for more tasks." : ""}`);
  }, true));
  byId("refresh-selected").addEventListener("click", () => perform(async () => { await selectTask(selected.task_id); notice("Task and history refreshed. Current revision loaded."); }));
  byId("load-more-history").addEventListener("click", () => perform(async () => {
    if (!selected || !historyHasMore) return;
    const history = await request(`tasks/${encodeURIComponent(selected.task_id)}/history?limit=100&offset=${historyOffset}`);
    appendHistory(history); notice(`${history.length} more history event(s) loaded.`);
  }));
  create.addEventListener("submit", (event) => {
    event.preventDefault();
    perform(async () => {
      const task = await request("tasks", { method: "POST", body: {
        task_id: byId("new-id").value, title: byId("new-title").value, description: byId("new-description").value,
      } });
      create.reset(); await loadTasks(); await loadBoard(); await selectTask(task.task_id); notice("Task created. Execution is not authorized.");
    });
  });
  edit.addEventListener("submit", (event) => {
    event.preventDefault();
    if (!selected || stale) return;
    let acceptance, architecture;
    try {
      acceptance = JSON.parse(byId("edit-acceptance").value);
      architecture = JSON.parse(byId("edit-architecture").value);
      if (![acceptance, architecture].every((items) => Array.isArray(items) && items.every((item) => typeof item === "string"))) throw new Error();
    } catch {
      notice("Acceptance criteria and architecture references must be JSON arrays of strings.", true);
      return;
    }
    perform(async () => {
      const body = {
        title: byId("edit-title").value, description: byId("edit-description").value,
        next_action: byId("edit-next").value,
        responsible: byId("edit-responsible").value,
        dependencies: byId("edit-dependencies").value.split(/\r?\n/).filter((line) => line.trim()),
        acceptance_criteria: acceptance,
        architecture_refs: architecture,
      };
      await request(`tasks/${encodeURIComponent(selected.task_id)}`, { method: "PATCH", body, revision: selected.revision });
      await loadTasks(); await loadBoard(); await selectTask(selected.task_id); notice("Task changes saved.");
    }, true);
  });
  action.addEventListener("submit", (event) => {
    event.preventDefault();
    if (!selected || stale) return;
    perform(async () => {
      const kind = byId("action-kind").value;
      const body = {reason: byId("action-reason").value};
      if (byId("action-next").value) body.next_action = byId("action-next").value;
      if (kind === "defer") {
        if (byId("action-until").value) body.until = byId("action-until").value;
        if (byId("action-milestone").value) body.milestone_task_id = byId("action-milestone").value;
      }
      await request(`tasks/${encodeURIComponent(selected.task_id)}/actions/${kind}`,
                    {method: "POST", body, revision: selected.revision});
      action.reset(); await loadTasks(); await loadBoard(); await selectTask(selected.task_id); notice("Task action recorded.");
    }, true);
  });
  function clearStructuralPreview() {
    structuralPlan = null;
    byId("structure-preview").textContent = "Plan changed. Preview mapping again.";
    controls();
  }
  byId("structure-kind").addEventListener("change", clearStructuralPreview);
  byId("structure-plan").addEventListener("input", clearStructuralPreview);
  byId("preview-structure").addEventListener("click", () => perform(async () => {
    if (!selected || stale) return;
    try {
      const planText = byId("structure-plan").value;
      const body = JSON.parse(planText);
      const kind = byId("structure-kind").value;
      const taskId = selected.task_id, revision = selected.revision;
      if (!body || typeof body !== "object" || Array.isArray(body) || typeof body.reason !== "string") throw new Error();
      let summary;
      if (kind === "split") {
        if (!Array.isArray(body.children) || body.children.length < 2 || body.children.length > 10 ||
            !body.incoming || typeof body.incoming !== "object" || Array.isArray(body.incoming)) throw new Error();
        const ids = body.children.map((child) => child.task_id);
        if (ids.some((id) => typeof id !== "string")) throw new Error();
        const missing = (field) => (selected[field] || []).filter((item) => !body.children.some((child) => (child[field] || []).includes(item)));
        summary = `Split ${selected.task_id} at revision ${selected.revision}\nChildren: ${ids.join(", ")}\nIncoming dependents: ${Object.keys(body.incoming).join(", ") || "none"}\nUnallocated acceptance: ${missing("acceptance_criteria").join(", ") || "none"}\nUnallocated architecture refs: ${missing("architecture_refs").join(", ") || "none"}\nUnallocated prerequisites: ${missing("dependencies").join(", ") || "none"}`;
      } else if (kind === "merge") {
        if (!Array.isArray(body.source_task_ids) || body.source_task_ids.length < 2 || body.source_task_ids.length > 10 ||
            !body.source_task_ids.includes(selected.task_id) || !body.target || !Array.isArray(body.incoming_dependents)) throw new Error();
        if (body.expected_revisions?.[selected.task_id] !== selected.revision) throw new Error();
        const sources = await Promise.all(body.source_task_ids.map((id) =>
          id === selected.task_id ? selected : request(`tasks/${encodeURIComponent(id)}`)));
        if (sources.some((source, index) => source.revision !== body.expected_revisions[body.source_task_ids[index]])) throw new Error();
        const required = (field) => [...new Set(sources.flatMap((source) => source[field] || []))];
        const missing = (field) => required(field).filter((item) => !(body.target[field] || []).includes(item));
        const externalDependencies = required("dependencies").filter((id) => !body.source_task_ids.includes(id));
        const missingDependencies = externalDependencies.filter((id) => !(body.target.dependencies || []).includes(id));
        summary = `Merge ${body.source_task_ids.join(", ")} into ${body.target.task_id}\nSource revisions: ${JSON.stringify(body.expected_revisions)}\nSource requirements:\n${JSON.stringify(sources.map((source) => ({task_id: source.task_id, acceptance_criteria: source.acceptance_criteria, architecture_refs: source.architecture_refs, dependencies: source.dependencies})), null, 2)}\nIncoming dependents: ${body.incoming_dependents.join(", ") || "none"}\nUnallocated acceptance: ${missing("acceptance_criteria").join(", ") || "none"}\nUnallocated architecture refs: ${missing("architecture_refs").join(", ") || "none"}\nUnallocated prerequisites: ${missingDependencies.join(", ") || "none"}`;
      } else throw new Error();
      if (planText !== byId("structure-plan").value || kind !== byId("structure-kind").value ||
          !selected || taskId !== selected.task_id || revision !== selected.revision || stale) return;
      structuralPlan = {kind, body, taskId, revision};
      byId("structure-preview").textContent = `${summary}\n\nExact request:\n${JSON.stringify(body, null, 2)}`;
      notice("Mapping preview ready. Check source IDs, revisions, acceptance, references and incoming dependents before applying.");
    } catch (error) {
      if (error instanceof ApiError) throw error;
      structuralPlan = null;
      byId("structure-preview").textContent = "Invalid plan. Check JSON and required fields.";
      notice("Invalid structural plan. Check JSON and required fields.", true);
    }
  }));
  structure.addEventListener("submit", (event) => {
    event.preventDefault();
    if (!selected || stale || !structuralPlan || structuralPlan.taskId !== selected.task_id || structuralPlan.revision !== selected.revision) return;
    perform(async () => {
      const {kind, body} = structuralPlan;
      const path = kind === "split" ? `tasks/${encodeURIComponent(selected.task_id)}/split` : "tasks/merge";
      const result = await request(path, {method: "POST", body, revision: kind === "split" ? selected.revision : undefined});
      structure.reset(); structuralPlan = null;
      await loadTasks(); await loadBoard(); await selectTask(kind === "split" ? result.children[0].task_id : result.target.task_id);
      notice("Structural task mapping recorded.");
    }, true);
  });
  controls();
})();
