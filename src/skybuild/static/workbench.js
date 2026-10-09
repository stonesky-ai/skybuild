"use strict";

(() => {
  const byId = (id) => document.getElementById(id);
  const connection = byId("connection-form");
  const create = byId("create-form");
  const edit = byId("edit-form");
  let token = "", project = "", selected = null, busy = false, stale = false, epoch = 0, controller = null;

  class ApiError extends Error {
    constructor(status) { super(`Request failed (${status})`); this.status = status; }
  }

  function notice(message, error = false) {
    byId("notice").textContent = message;
    byId("notice").classList.toggle("error", error);
  }

  function controls() {
    const connected = Boolean(token);
    byId("project").disabled = connected || busy;
    byId("token").disabled = connected || busy;
    byId("connect").disabled = connected || busy;
    byId("logout").disabled = !connected;
    byId("refresh-tasks").disabled = !connected || busy;
    byId("refresh-selected").disabled = !connected || busy || !selected;
    for (const field of create.elements) field.disabled = !connected || busy;
    for (const field of edit.elements) field.disabled = !connected || busy || !selected;
    byId("save").disabled = !connected || busy || !selected || stale;
    for (const button of byId("task-list").querySelectorAll("button")) button.disabled = busy || !connected;
  }

  function disconnect() {
    epoch += 1;
    if (controller) controller.abort();
    token = ""; project = ""; selected = null; stale = false; busy = false;
    byId("project").value = "";
    byId("token").value = "";
    create.reset(); edit.reset();
    byId("task-list").replaceChildren(); byId("history").replaceChildren();
    byId("task-count").textContent = "Not connected";
    byId("selection").textContent = "Select a task to view its definition and history.";
    controls();
  }

  async function request(suffix, options = {}) {
    const session = epoch;
    const activeController = new AbortController();
    controller = activeController;
    const headers = { Authorization: `Bearer ${token}` };
    if (options.body !== undefined) {
      headers["Content-Type"] = "application/json";
      headers["Idempotency-Key"] = crypto.randomUUID();
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

  async function perform(operation, mutation = false) {
    if (busy) return;
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

  async function loadTasks() {
    const tasks = await request("tasks?limit=100&offset=0");
    const list = byId("task-list"); list.replaceChildren();
    for (const task of tasks) {
      const item = document.createElement("li"), button = document.createElement("button");
      button.type = "button";
      button.dataset.taskId = task.task_id;
      button.textContent = `${task.task_id}: ${task.title} (${task.status})`;
      button.setAttribute("aria-current", String(selected?.task_id === task.task_id));
      button.addEventListener("click", () => perform(async () => { await selectTask(task.task_id); notice("Task loaded."); }));
      item.append(button); list.append(item);
    }
    byId("task-count").textContent = `${tasks.length} tasks shown for ${project}`;
  }

  async function selectTask(taskId) {
    const path = `tasks/${encodeURIComponent(taskId)}`;
    const task = await request(path);
    const history = await request(`${path}/history?limit=100&offset=0`);
    selected = task; stale = false;
    byId("selection").textContent = `${task.task_id} · Revision ${task.revision}`;
    for (const [id, field] of [
      ["edit-title", "title"], ["edit-description", "description"], ["edit-status", "status"],
      ["edit-phase", "phase"], ["edit-next", "next_action"], ["edit-blocker", "blocker"], ["edit-responsible", "responsible"],
    ]) byId(id).value = task[field] ?? "";
    byId("edit-dependencies").value = task.dependencies.join("\n");
    const events = byId("history"); events.replaceChildren();
    for (const event of history) {
      const item = document.createElement("li"), summary = document.createElement("p");
      summary.textContent = `Revision ${event.revision} · ${event.actor} · ${event.operation} · ${event.created_at || event.at || ""} · ${event.reason || ""}`;
      const details = document.createElement("details"), label = document.createElement("summary"), body = document.createElement("pre");
      label.textContent = "Event details"; body.textContent = JSON.stringify(event, null, 2);
      details.append(label, body); item.append(summary, details); events.append(item);
    }
    for (const button of byId("task-list").querySelectorAll("button")) {
      button.setAttribute("aria-current", String(button.dataset.taskId === task.task_id));
    }
  }

  connection.addEventListener("submit", (event) => {
    event.preventDefault();
    token = byId("token").value; project = byId("project").value;
    byId("token").value = "";
    perform(async () => { await loadTasks(); notice(`Connected to ${project}.`); });
  });
  byId("logout").addEventListener("click", () => { disconnect(); notice("Logged out. Private task data and token cleared."); });
  byId("refresh-tasks").addEventListener("click", () => perform(async () => { await loadTasks(); notice("Task list refreshed."); }));
  byId("refresh-selected").addEventListener("click", () => perform(async () => { await selectTask(selected.task_id); notice("Task and history refreshed. Current revision loaded."); }));
  create.addEventListener("submit", (event) => {
    event.preventDefault();
    perform(async () => {
      const task = await request("tasks", { method: "POST", body: {
        task_id: byId("new-id").value, title: byId("new-title").value, description: byId("new-description").value,
      } });
      create.reset(); await loadTasks(); await selectTask(task.task_id); notice("Task created. Execution is not authorized.");
    });
  });
  edit.addEventListener("submit", (event) => {
    event.preventDefault();
    if (!selected || stale) return;
    perform(async () => {
      const body = {
        title: byId("edit-title").value, description: byId("edit-description").value,
        status: byId("edit-status").value, phase: byId("edit-phase").value,
        next_action: byId("edit-next").value, blocker: byId("edit-blocker").value,
        responsible: byId("edit-responsible").value,
        dependencies: byId("edit-dependencies").value.split(/\r?\n/).filter((line) => line.trim()),
      };
      await request(`tasks/${encodeURIComponent(selected.task_id)}`, { method: "PATCH", body, revision: selected.revision });
      await loadTasks(); await selectTask(selected.task_id); notice("Task changes saved.");
    }, true);
  });
  controls();
})();
