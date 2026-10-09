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
    metadata: {fake: true, waiting_on: ["FAKE-SKYBUILD-BOOTSTRAP"], will_enable: "TBD"}, fake: true,
  };
  const workflowSample = (taskId, title, current, steps) => {
    const task = {...fakeTask, task_id: taskId, title, status: current.status, phase: current.phase,
      next_action: current.next_action, blocker: current.blocker || "", revision: steps.length, fake: true};
    const history = steps.map((step, index) => ({
      event_id: `FAKE-${taskId}-${String(index + 1).padStart(3, "0")}`, task_id: taskId,
      actor: step.actor || "fake-preview", operation: step.operation, revision: index + 1,
      created_at: `2026-10-0${Math.min(index + 1, 9)}T15:00:00Z`, reason: step.reason,
      after_state: {...task, status: step.status, phase: step.phase, revision: index + 1},
      outcome: step.outcome, fake: true,
    }));
    return {task, history};
  };
  const fakeCases = [
    {task: fakeTask, history: [
      {event_id: "FAKE-EVENT-001", task_id: fakeTask.task_id, actor: "fake-owner", operation: "created", revision: 1,
        created_at: "2026-10-01T14:10:00Z", reason: "Fake preview task created", after_state: {...fakeTask, status: "proposed", phase: "planned", revision: 1}, fake: true},
      {event_id: "FAKE-EVENT-002", task_id: fakeTask.task_id, actor: "fake-owner", operation: "readied", revision: 2,
        created_at: "2026-10-01T14:30:00Z", reason: "Definition and acceptance are sufficient", after_state: {...fakeTask, status: "ready", phase: "ready", revision: 2}, fake: true},
      {event_id: "FAKE-EVENT-003", task_id: fakeTask.task_id, actor: "fake-worker", operation: "claimed", revision: 3,
        created_at: fakeTimestamp, reason: "Fake implementation started; commit evidence attached", after_state: {...fakeTask, status: "in-progress", phase: "working", revision: 3},
        commits: [fakeCommit], fake: true},
    ]},
    workflowSample("FAKE-REVIEW-READY", "Review passed; integration assessment next", {status: "in-progress", phase: "assessing"}, [
      {operation: "created", status: "proposed", phase: "planned", reason: "Fake task created"},
      {operation: "readied", status: "ready", phase: "ready", reason: "Definition accepted"},
      {operation: "claimed", status: "in-progress", phase: "working", reason: "Author claimed task"},
      {operation: "validated", status: "in-progress", phase: "validating", reason: "Required checks passed"},
      {operation: "reviewed", status: "in-progress", phase: "reviewing", outcome: "passed", reason: "Independent review passed"},
      {operation: "assessment_started", status: "in-progress", phase: "assessing", reason: "Assessing integration readiness"},
    ]),
    workflowSample("FAKE-REWORK-BLOCKED", "Rework blocked on dependency", {status: "blocked", phase: "blocked", blocker: "Dependency evidence missing", next_action: "Owner resolves dependency"}, [
      {operation: "created", status: "proposed", phase: "planned", reason: "Fake task created"},
      {operation: "readied", status: "ready", phase: "ready", reason: "Definition accepted"},
      {operation: "claimed", status: "in-progress", phase: "working", reason: "Author started implementation"},
      {operation: "check_failed", status: "in-progress", phase: "needs-rework", outcome: "failed", reason: "A required check found a code failure"},
      {operation: "rework_started", status: "in-progress", phase: "working", reason: "Bounded correction started"},
      {operation: "implementation_result", status: "in-progress", phase: "validating", reason: "Correction committed for validation"},
      {operation: "checks_passed", status: "in-progress", phase: "reviewing", reason: "Required checks now pass"},
      {operation: "review_passed", status: "in-progress", phase: "assessing", reason: "Independent review passed; assess dependency"},
      {operation: "blocked", status: "blocked", phase: "blocked", outcome: "blocked", reason: "Dependency evidence missing; owner action required"},
    ]),
    workflowSample("FAKE-DEFERRED", "Deferred until prerequisite completes", {status: "deferred", phase: "deferred", next_action: "Resume after prerequisite completes"}, [
      {operation: "created", status: "proposed", phase: "planned", reason: "Fake task created"},
      {operation: "readied", status: "ready", phase: "ready", reason: "Definition accepted"},
      {operation: "deferred", status: "deferred", phase: "deferred", reason: "Waiting for prerequisite milestone"},
    ]),
    workflowSample("FAKE-DONE", "Published and accepted", {status: "done", phase: "done", next_action: "Complete"}, [
      {operation: "created", status: "proposed", phase: "planned", reason: "Fake task created"},
      {operation: "readied", status: "ready", phase: "ready", reason: "Definition accepted"},
      {operation: "claimed", status: "in-progress", phase: "working", reason: "Author completed implementation"},
      {operation: "validated", status: "in-progress", phase: "validating", reason: "Required checks passed"},
      {operation: "reviewed", status: "in-progress", phase: "reviewing", outcome: "passed", reason: "Independent review passed"},
      {operation: "assessment_started", status: "in-progress", phase: "assessing", reason: "Head and prerequisites assessed"},
      {operation: "bundle_ready", status: "in-progress", phase: "ready-for-bundle", reason: "Exact head accepted for bundle"},
      {operation: "integrating", status: "in-progress", phase: "integrating", reason: "Bundle gates passed and publication started"},
      {operation: "completed", status: "done", phase: "done", outcome: "passed", reason: "Publication and acceptance confirmed"},
    ]),
  ];
  const fakeHistoryByTask = new Map(fakeCases.map(sample => [sample.task.task_id, sample.history]));
  const fakeTasks = fakeCases.map(sample => sample.task);

  let token = previewMode ? "local-preview" : "", project = previewMode ? "skybuild" : "", tasks = [], selectedTask = null, selectedHistory = [], cursor = null;
  let historyOffset = 0, historyHasMore = false, busy = false, epoch = 0;
  const taskColumns = [
    {key: "task", label: "Task", value: task => `${task.title || "—"} · ${task.task_id || "—"}`},
    {key: "status", label: "Current status", value: task => task.status},
    {key: "next", label: "Next status", value: task => task.status === "deferred" ? "blocked on resume" : task.status === "done" ? "none (complete)" : "Not recorded by API"},
    {key: "waiting", label: "Waiting on", value: task => taskRelation(task, "waiting_on"),
      filterValue: task => relationToken(taskRelation(task, "waiting_on")),
      filterLabel: task => relationLabel(taskRelation(task, "waiting_on"))},
    {key: "enables", label: "Will Enable", value: task => taskRelation(task, "will_enable"),
      filterValue: task => relationToken(taskRelation(task, "will_enable")),
      filterLabel: task => relationLabel(taskRelation(task, "will_enable"))},
    {key: "priority", label: "Priority", value: task => task.priority},
    {key: "action", label: "Next action / blocker", value: task => task.blocker || task.next_action},
    {key: "responsible", label: "Responsible", value: task => task.responsible},
    {key: "updated", label: "Updated", value: task => task.updated_at},
    {key: "record", label: "All information", value: task => task.fake ? "Fake preview data" : "API task"},
  ];
  let sortColumns = [];
  const taskFilters = new Map();
  const taskCollator = new Intl.Collator(undefined, {numeric: true, sensitivity: "base"});

  function taskRelation(task, name) {
    const metadata = task.metadata && typeof task.metadata === "object" && !Array.isArray(task.metadata) ? task.metadata : {};
    if (Object.prototype.hasOwnProperty.call(task, name)) return task[name] ?? "TBD";
    if (Object.prototype.hasOwnProperty.call(metadata, name)) return metadata[name] ?? "TBD";
    // `dependencies` is the task record's declared prerequisite list. Will-enable stays explicitly recorded;
    // never infer it by scanning other tasks because that would hide missing or stale task data.
    if (name === "waiting_on" && Array.isArray(task.dependencies)) return task.dependencies;
    return "TBD";
  }

  function relationItems(value) {
    if (Array.isArray(value)) return value.map(item => typeof item === "object" && item ? item.task_id || item.id || "TBD" : String(item));
    if (value === null || value === undefined || value === "" || String(value).toUpperCase() === "TBD") return null;
    return [String(value)];
  }

  function relationToken(value) {
    const items = relationItems(value);
    return items === null ? "@tbd" : JSON.stringify([...items].sort(taskCollator.compare));
  }

  function relationLabel(value) {
    const items = relationItems(value);
    if (items === null) return "TBD";
    if (!items.length) return "None";
    const count = `${items.length} task${items.length === 1 ? "" : "s"}`;
    return `${count}: ${items.slice(0, 2).join(", ")}${items.length > 2 ? ", …" : ""}`;
  }

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
    byId("project").disabled = (connected && !previewMode) || busy;
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

  function renderTaskHead() {
    const head = byId("task-head");
    const headings = document.createElement("tr");
    for (const column of taskColumns) {
      const cell = document.createElement("th");
      const order = sortColumns.findIndex(item => item.key === column.key);
      const direction = order < 0 ? "" : sortColumns[order].direction;
      cell.setAttribute("aria-sort", order === 0 ? (direction === "asc" ? "ascending" : "descending") : "none");
      const sort = document.createElement("button");
      sort.type = "button";
      sort.className = "task-sort-button";
      sort.title = "Click to sort. Control-click to add or change a secondary sort.";
      sort.setAttribute("aria-label", `Sort by ${column.label}${order < 0 ? "" : `, ${direction === "asc" ? "ascending" : "descending"}, priority ${order + 1}`}`);
      sort.append(document.createTextNode(column.label));
      const marker = document.createElement("span");
      marker.className = "sort-marker";
      marker.textContent = order < 0 ? "" : `${direction === "asc" ? "▲" : "▼"}${sortColumns.length > 1 ? order + 1 : ""}`;
      sort.append(marker);
      sort.addEventListener("click", event => {
        const existing = sortColumns.find(item => item.key === column.key);
        const nextDirection = existing ? (existing.direction === "asc" ? "desc" : "asc") : "asc";
        if (event.ctrlKey) {
          sortColumns = existing
            ? sortColumns.map(item => item.key === column.key ? {...item, direction: nextDirection} : item)
            : [...sortColumns, {key: column.key, direction: nextDirection}];
        } else {
          sortColumns = [{key: column.key, direction: nextDirection}];
        }
        renderTasks();
      });
      cell.append(sort);
      headings.append(cell);
    }
    const filters = document.createElement("tr");
    filters.className = "task-filter-row";
    for (const column of taskColumns) {
      const cell = document.createElement("th");
      const controls = document.createElement("div");
      controls.className = "task-column-filter";
      const select = document.createElement("select");
      select.setAttribute("aria-label", `Filter ${column.label}`);
      const all = document.createElement("option");
      all.value = "";
      all.textContent = "All";
      select.append(all);
      const options = new Map();
      for (const task of tasks) {
        const raw = column.filterValue ? column.filterValue(task) : column.value(task);
        const value = raw === null || raw === undefined || raw === "" ? "—" : String(raw);
        const label = column.filterLabel ? column.filterLabel(task) : value;
        if (!options.has(value)) options.set(value, label);
      }
      const selected = taskFilters.get(column.key) || "";
      if (selected && !options.has(selected)) options.set(selected, selected === "@tbd" ? "TBD" : selected);
      for (const [value, label] of [...options.entries()].sort((a, b) => taskCollator.compare(a[1], b[1]))) {
        const option = document.createElement("option");
        option.value = value;
        option.textContent = label.length > 44 ? label.slice(0, 43) + "…" : label;
        option.title = label;
        select.append(option);
      }
      select.value = selected;
      select.addEventListener("change", () => {
        if (select.value) taskFilters.set(column.key, select.value);
        else taskFilters.delete(column.key);
        renderTasks();
      });
      const clear = document.createElement("button");
      clear.type = "button";
      clear.className = "task-filter-clear";
      clear.textContent = "×";
      clear.title = `Clear ${column.label} filter`;
      clear.setAttribute("aria-label", `Clear ${column.label} filter`);
      clear.disabled = !selected;
      clear.addEventListener("click", () => {
        taskFilters.delete(column.key);
        renderTasks();
      });
      controls.append(select, clear);
      cell.append(controls);
      filters.append(cell);
    }
    head.replaceChildren(headings, filters);
  }

  function filteredSortedTasks() {
    const visible = tasks.filter(task => taskColumns.every(column => {
      if (!taskFilters.has(column.key)) return true;
      const value = column.value(task);
      const raw = column.filterValue ? column.filterValue(task) : value;
      return (raw === null || raw === undefined || raw === "" ? "—" : String(raw)) === taskFilters.get(column.key);
    }));
    const columns = new Map(taskColumns.map(column => [column.key, column]));
    visible.sort((left, right) => {
      for (const item of sortColumns) {
        const column = columns.get(item.key);
        const aRaw = column.value(left), bRaw = column.value(right);
        const a = Array.isArray(aRaw) ? [...aRaw].join(", ") : aRaw;
        const b = Array.isArray(bRaw) ? [...bRaw].join(", ") : bRaw;
        const aMissing = a === null || a === undefined || a === "";
        const bMissing = b === null || b === undefined || b === "";
        if (aMissing !== bMissing) return aMissing ? 1 : -1;
        const order = typeof a === "number" && typeof b === "number" ? a - b : taskCollator.compare(String(a ?? ""), String(b ?? ""));
        if (order) return item.direction === "asc" ? order : -order;
      }
      return taskCollator.compare(String(left.task_id || ""), String(right.task_id || ""));
    });
    return visible;
  }

  function renderTasks() {
    const rows = byId("task-rows");
    rows.replaceChildren();
    renderTaskHead();
    const visibleTasks = filteredSortedTasks();
    visibleTasks.forEach(task => {
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
      appendTaskRelationCell(row, taskRelation(task, "waiting_on"));
      appendTaskRelationCell(row, taskRelation(task, "will_enable"));
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
    const filteredCount = taskFilters.size ? ` · showing ${visibleTasks.length} of ${tasks.length}` : "";
    byId("task-count").textContent = previewMode && project === "skykeep"
      ? "SkyKeep repo is listed but not configured in this preview."
      : previewMode
        ? `${tasks.length} fake task${tasks.length === 1 ? "" : "s"} loaded · signed in as user1${filteredCount}`
      : !token ? "Not connected"
        : `${tasks.length} task${tasks.length === 1 ? "" : "s"} loaded for ${project}${fake ? " · API returned no tasks; showing fake preview records" : ""}${filteredCount}`;
    renderTaskPicker();
    controls();
  }

  function appendTaskRelationCell(row, value) {
    const cell = document.createElement("td");
    const items = relationItems(value);
    if (items === null) {
      cell.textContent = "TBD";
    } else if (!items.length) {
      cell.textContent = "None";
    } else {
      const details = document.createElement("details");
      const summary = document.createElement("summary");
      summary.textContent = `${items.length} task${items.length === 1 ? "" : "s"}`;
      const list = document.createElement("ul");
      for (const id of items) {
        const line = document.createElement("li");
        const known = tasks.find(task => task.task_id === id);
        line.textContent = known ? `${id} · ${known.title}` : id;
        list.append(line);
      }
      details.append(summary, list);
      cell.append(details);
    }
    row.append(cell);
    return cell;
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
      if (project === "skykeep") {
        cursor = null;
        tasks = [];
        renderTasks();
        return;
      }
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
      ["Waiting on", taskRelation(task, "waiting_on")], ["Will enable", taskRelation(task, "will_enable")],
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

  const workflowPlaces = [
    {id: "planned", label: "Planned", x: 30, y: 58}, {id: "ready", label: "Ready for work", x: 160, y: 58},
    {id: "working", label: "Working", x: 290, y: 58}, {id: "validating", label: "Validating", x: 420, y: 58},
    {id: "reviewing", label: "Reviewing", x: 550, y: 58}, {id: "assessing", label: "Assessing integration", x: 680, y: 58},
    {id: "bundle", label: "Ready for bundle", x: 810, y: 58}, {id: "integrating", label: "Integrating", x: 940, y: 58},
    {id: "done", label: "Done", x: 1070, y: 58},
    {id: "rework", label: "Needs rework", x: 420, y: 218}, {id: "rebase", label: "Needs rebase", x: 680, y: 218},
    {id: "blocked", label: "Blocked", x: 810, y: 218}, {id: "reassess", label: "Reassess", x: 940, y: 218},
    {id: "deferred", label: "Deferred", x: 1070, y: 218}, {id: "superseded", label: "Superseded", x: 160, y: 218},
    {id: "replacement", label: "Replacement", x: 290, y: 218},
  ];
  const workflowTransitions = [
    {id: "T1", from: "planned", to: "ready", label: "Definition sufficient", actor: "Owner"},
    {id: "T2", from: "ready", to: "working", label: "Admitted and claimed", actor: "Admission / worker"},
    {id: "T3", from: "working", to: "validating", label: "Implementation result", actor: "Author"},
    {id: "T4", from: "validating", to: "reviewing", label: "Required checks pass", actor: "Check runner"},
    {id: "T5", from: "reviewing", to: "assessing", label: "Required reviews pass", actor: "Independent reviewer"},
    {id: "T6", from: "assessing", to: "bundle", label: "Head and prerequisites ready", actor: "Integrator"},
    {id: "T7", from: "bundle", to: "integrating", label: "Fixed bundle selected", actor: "Integrator"},
    {id: "T8", from: "integrating", to: "done", label: "Publication and acceptance confirmed", actor: "Publisher / acceptance owner"},
    {id: "T9", from: "validating", to: "rework", label: "Diagnosed code failure", actor: "Check runner / author"},
    {id: "T10", from: "reviewing", to: "rework", label: "Actionable fixes requested", actor: "Independent reviewer"},
    {id: "T11", from: "rework", to: "working", label: "Rework admitted", actor: "Admission / author"},
    {id: "T12", from: "assessing", to: "rebase", label: "Base conflict or rebase required", actor: "Integrator"},
    {id: "T13", from: "rebase", to: "validating", label: "Rebase done; evidence invalidated", actor: "Author / reconciler"},
    {id: "T14", from: "assessing", to: "blocked", label: "Prerequisite missing or cause unknown", actor: "Resolver"},
    {id: "T15", from: "blocked", to: "reassess", label: "Blocker resolved", actor: "Resolver"},
    {id: "T16", from: "deferred", to: "reassess", label: "Trigger reached or owner resumes", actor: "Owner / CPU reconciler"},
    {id: "T17", from: "assessing", to: "done", label: "Acceptance met without integration", actor: "Acceptance owner"},
    {id: "T18", from: "integrating", to: "rework", label: "Diagnosed integration correction", actor: "Integrator / author"},
    {id: "T19", from: "integrating", to: "assessing", label: "Candidate changed or excluded", actor: "Integrator"},
    {id: "T20", from: "integrating", to: "integrating", label: "Gate running or publication uncertain", actor: "Integrator"},
    {id: "T21", from: "done", to: "rework", label: "Rework requested", actor: "Owner / author"},
    {id: "T22", from: "replacement", to: "reassess", label: "Assess replacement task", actor: "Owner / reconciler"},
    {id: "T23", from: "reassess", to: "planned", label: "Definition incomplete", actor: "Owner / reconciler"},
    {id: "T24", from: "reassess", to: "ready", label: "Implementation needed", actor: "Owner / reconciler"},
    {id: "T25", from: "reassess", to: "validating", label: "Checks stale or missing", actor: "CPU reconciler"},
    {id: "T26", from: "reassess", to: "reviewing", label: "Review stale or missing", actor: "CPU reconciler"},
    {id: "T27", from: "reassess", to: "assessing", label: "Ready to assess integration", actor: "CPU reconciler"},
    {id: "T28", from: "reassess", to: "blocked", label: "Known blocker remains", actor: "Resolver"},
    {id: "T29", from: "planned", outputs: ["superseded", "replacement"], label: "Split or merge proposed work", actor: "Owner"},
    {id: "T30", from: "ready", to: "deferred", label: "Defer until date or milestone", actor: "Owner"},
    {id: "T31", from: "ready", to: "reassess", label: "Definition or dependency changed", actor: "Owner / reconciler"},
    {id: "T32", from: "planned", to: "planned", label: "Planning scan proposes changes", actor: "CPU / qualified model"},
  ];
  const workflowPlaceNotes = {
    planned: "Definition or acceptance still needs work.", ready: "Definition is sufficient; admission is still separate.",
    working: "Work is admitted and claimed.", validating: "Current required checks are running or need evidence.",
    reviewing: "Independent review is running or needs evidence.", assessing: "Check current head, prerequisites, and integration readiness.",
    bundle: "Current head and prerequisites qualify for a fixed bundle.", integrating: "Bundle gate or publication is in progress or uncertain.",
    done: "Configured acceptance and any required publication are confirmed.", rework: "A diagnosed correction or requested change remains.",
    rebase: "Base conflict requires rebase; old evidence is invalidated.", blocked: "Wait condition with a named resolver; underlying phase may be retained.",
    reassess: "Re-evaluate the earliest unmet requirement after inputs change.", deferred: "Wait for a recorded date, milestone, or owner resume.",
    superseded: "This task was replaced; retain history and lineage.", replacement: "Linked task created by split or merge; reassess its definition.",
  };
  const workflowAliases = new Map([
    ["planned", "planned"], ["proposed", "planned"], ["created", "planned"],
    ["ready", "ready"], ["ready_for_work", "ready"], ["ready-for-work", "ready"],
    ["working", "working"], ["claimed", "working"], ["in-progress", "working"], ["in_progress", "working"],
    ["validating", "validating"], ["in-testing", "validating"], ["in_testing", "validating"], ["testing", "validating"], ["validated", "reviewing"],
    ["reviewing", "reviewing"], ["in-review", "reviewing"], ["in_review", "reviewing"], ["ready-for-review", "reviewing"], ["ready_for_review", "reviewing"],
    ["assessing", "assessing"], ["assessing-integration-readiness", "assessing"], ["assessment", "assessing"],
    ["ready-for-bundle", "bundle"], ["ready_for_bundle", "bundle"], ["bundle-ready", "bundle"],
    ["integrating", "integrating"], ["done", "done"], ["completed", "done"],
    ["needs-rework", "rework"], ["needs_rework", "rework"], ["rework", "rework"],
    ["needs-rebase", "rebase"], ["needs_rebase", "rebase"], ["rebase", "rebase"],
    ["blocked", "blocked"], ["deferred", "deferred"], ["reassessing", "reassess"], ["reassess", "reassess"],
    ["superseded", "superseded"], ["split", "split"], ["merged", "replacement"],
  ]);

  function workflowStage(value) {
    if (value === null || value === undefined) return null;
    const key = String(value).trim().toLowerCase().replaceAll(" ", "-");
    return workflowAliases.get(key) || workflowAliases.get(key.replaceAll("-", "_")) || null;
  }

  function journalStage(event) {
    const state = event.after_state || event.state || {};
    const phase = workflowStage(state.phase) || workflowStage(event.phase);
    if (phase) return phase;
    const status = workflowStage(state.status) || workflowStage(event.status);
    if (status) return status;
    const operation = String(event.operation || "").toLowerCase().replaceAll("_", "-");
    return workflowStage(operation);
  }

  function renderWorkflowTables() {
    const placeRows = byId("workflow-place-rows");
    const transitionRows = byId("workflow-transition-rows");
    if (!placeRows || !transitionRows) return;
    placeRows.replaceChildren(); transitionRows.replaceChildren();
    for (const place of workflowPlaces) {
      const row = document.createElement("tr");
      for (const value of [`${place.id} · ${place.label}`, workflowPlaceNotes[place.id]]) {
        const cell = document.createElement("td"); cell.textContent = value; row.append(cell);
      }
      placeRows.append(row);
    }
    const labels = new Map(workflowPlaces.map(place => [place.id, `${place.id} · ${place.label}`]));
    for (const transition of workflowTransitions) {
      const row = document.createElement("tr");
      const outputs = transition.outputs || [transition.to];
      const values = [labels.get(transition.from) || transition.from, `${transition.id} · ${transition.label}`,
        outputs.map(id => labels.get(id) || id).join(" + "), transition.actor];
      for (const value of values) { const cell = document.createElement("td"); cell.textContent = value; row.append(cell); }
      transitionRows.append(row);
    }
  }

  function renderWorkflowChart() {
    const target = byId("workflow-chart");
    target.replaceChildren();
    renderWorkflowTables();
    if (!selectedTask) {
      byId("workflow-caption").textContent = "Select a task to see its journaled path through the workflow.";
      target.textContent = "Select a task to load its workflow history.";
      return;
    }
    const ordered = [...selectedHistory].sort((a, b) => Number(a.revision || 0) - Number(b.revision || 0));
    const reached = new Set(), failed = new Set(), failedTransitions = new Set(), traversedTransitions = new Set();
    let previousStage = null;
    for (const event of ordered) {
      const stage = journalStage(event);
      if (!stage) continue;
      reached.add(stage);
      const outcome = String(event.outcome || event.result || event.review?.result || "").toLowerCase();
      const transition = previousStage && workflowTransitions.find(item => item.from === previousStage && (item.to === stage || item.outputs?.includes(stage)));
      if (transition) traversedTransitions.add(transition.id);
      if (["failed", "failure", "rejected"].includes(outcome) || ["rework", "rebase"].includes(stage)) {
        failed.add(stage);
        if (transition) failedTransitions.add(transition.id);
      }
      previousStage = stage;
    }
    const currentState = [...ordered].reverse().find(event => event.after_state || event.state);
    const state = currentState?.after_state || currentState?.state || selectedTask;
    let current = workflowStage(state.phase) || workflowStage(state.status) || workflowStage(selectedTask.phase) || workflowStage(selectedTask.status);
    if ((state.status === "blocked" || selectedTask.status === "blocked") && !workflowStage(state.phase)) current = "blocked";
    if ((state.status === "deferred" || selectedTask.status === "deferred") && !workflowStage(state.phase)) current = "deferred";
    if (state.status === "superseded" || selectedTask.status === "superseded") current = "superseded";
    if (state.status === "done" || selectedTask.status === "done") current = "done";
    if (current && !reached.has(current)) reached.add(current);
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 1220 310");
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-labelledby", "workflow-svg-title workflow-svg-desc");
    const title = document.createElementNS(svg.namespaceURI, "title"); title.id = "workflow-svg-title";
    title.textContent = `Workflow path for ${selectedTask.task_id}`;
    const desc = document.createElementNS(svg.namespaceURI, "desc"); desc.id = "workflow-svg-desc";
    desc.textContent = `Petri-net sketch. Circles are task-state places; bars are named action transitions. Current state: ${workflowPlaces.find(node => node.id === current)?.label || "not mapped"}.`;
    svg.append(title, desc);
    const defs = document.createElementNS(svg.namespaceURI, "defs");
    const marker = document.createElementNS(svg.namespaceURI, "marker");
    marker.id = "workflow-arrow"; marker.setAttribute("viewBox", "0 0 10 10"); marker.setAttribute("refX", "8");
    marker.setAttribute("refY", "5"); marker.setAttribute("markerWidth", "5"); marker.setAttribute("markerHeight", "5"); marker.setAttribute("orient", "auto-start-reverse");
    const arrow = document.createElementNS(svg.namespaceURI, "path"); arrow.setAttribute("d", "M 0 0 L 10 5 L 0 10 z"); arrow.setAttribute("fill", "context-stroke");
    marker.append(arrow); defs.append(marker); svg.append(defs);
    const nodeMap = new Map(workflowPlaces.map(node => [node.id, node]));
    const transitionPositions = workflowTransitions.map(transition => {
      const from = nodeMap.get(transition.from), outputs = transition.outputs || [transition.to];
      const targets = outputs.map(id => nodeMap.get(id)).filter(Boolean);
      const cx = from.x + 58, cy = from.y + 24;
      const tx = targets.reduce((sum, node) => sum + node.x + 58, 0) / targets.length;
      const ty = targets.reduce((sum, node) => sum + node.y + 24, 0) / targets.length;
      const loop = transition.from === outputs[0];
      return {...transition, x: loop ? cx + 44 : (cx + tx) / 2, y: loop ? cy - 42 : (cy + ty) / 2, outputs};
    });
    const transitionMap = new Map(transitionPositions.map(item => [item.id, item]));
    const edgeGroup = document.createElementNS(svg.namespaceURI, "g"); edgeGroup.setAttribute("class", "workflow-edges");
    const drawArc = (from, to, className) => {
      const path = document.createElementNS(svg.namespaceURI, "path");
      path.setAttribute("d", `M ${from.x} ${from.y} L ${to.x} ${to.y}`);
      path.setAttribute("class", className); path.setAttribute("marker-end", "url(#workflow-arrow)"); edgeGroup.append(path);
    };
    for (const transition of transitionPositions) {
      const source = nodeMap.get(transition.from);
      const tc = {x: transition.x, y: transition.y};
      const dx = tc.x - (source.x + 58), dy = tc.y - (source.y + 24), length = Math.hypot(dx, dy) || 1;
      const sourcePort = {x: source.x + 58 + dx / length * 37, y: source.y + 24 + dy / length * 37};
      const transitionPort = {x: tc.x - Math.sign(dx || 1) * 8, y: tc.y};
      const past = traversedTransitions.has(transition.id);
      const color = failedTransitions.has(transition.id) ? "failed" : past ? "reached" : "";
      drawArc(sourcePort, transitionPort, `workflow-edge ${color}`);
      for (const output of transition.outputs) {
        const destination = nodeMap.get(output);
        if (!destination) continue;
        const ox = destination.x + 58 - tc.x, oy = destination.y + 24 - tc.y, olen = Math.hypot(ox, oy) || 1;
        const actionPort = {x: tc.x + Math.sign(ox || 1) * 8, y: tc.y};
        const placePort = {x: destination.x + 58 - ox / olen * 37, y: destination.y + 24 - oy / olen * 37};
        drawArc(actionPort, placePort, `workflow-edge ${color}`);
      }
    }
    svg.append(edgeGroup);
    for (const transition of transitionPositions) {
      const stateClass = failedTransitions.has(transition.id) ? "failed"
        : traversedTransitions.has(transition.id) ? "done" : "waiting";
      const group = document.createElementNS(svg.namespaceURI, "g"); group.setAttribute("class", `workflow-transition ${stateClass}`);
      group.setAttribute("aria-label", `${transition.id}: ${transition.label}; actor ${transition.actor}`);
      const rect = document.createElementNS(svg.namespaceURI, "rect");
      rect.setAttribute("x", transition.x - 7); rect.setAttribute("y", transition.y - 19); rect.setAttribute("width", "14"); rect.setAttribute("height", "38"); rect.setAttribute("rx", "2");
      const text = document.createElementNS(svg.namespaceURI, "text"); text.setAttribute("x", transition.x); text.setAttribute("y", transition.y + 31); text.textContent = transition.id;
      group.append(rect, text); svg.append(group);
    }
    for (const node of workflowPlaces) {
      const group = document.createElementNS(svg.namespaceURI, "g");
      const stateClass = node.id === current ? "current" : failed.has(node.id) ? "failed" : reached.has(node.id) ? "done" : "waiting";
      group.setAttribute("class", `workflow-place ${stateClass}`);
      group.setAttribute("aria-label", `${node.label}: ${stateClass}`);
      const circle = document.createElementNS(svg.namespaceURI, "circle");
      circle.setAttribute("cx", node.x + 58); circle.setAttribute("cy", node.y + 24); circle.setAttribute("r", "36");
      const text = document.createElementNS(svg.namespaceURI, "text");
      text.setAttribute("x", node.x + 58); text.setAttribute("y", node.y + 72); text.textContent = node.label;
      group.append(circle, text); svg.append(group);
    }
    target.append(svg);
    const currentLabel = workflowPlaces.find(node => node.id === current)?.label || "State not mapped";
    const waitState = selectedTask.status === "blocked" ? "BLOCKED · " : selectedTask.status === "deferred" ? "DEFERRED · " : "";
    byId("workflow-caption").textContent = `${selectedTask.fake ? "FAKE JOURNAL · " : "Journal-derived · "}${waitState}${currentLabel}${selectedTask.blocker ? ` · ${selectedTask.blocker}` : ""}`;
  }

  function renderHistory() {
    byId("history-task").textContent = selectedTask
      ? `${selectedTask.fake ? "[FAKE PREVIEW] " : ""}${selectedTask.task_id} · ${selectedTask.title}`
      : "Choose a task from Task List.";
    const list = byId("history-list");
    list.replaceChildren();
    renderWorkflowChart();
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
      history = fakeHistoryByTask.get(task.task_id) || [{event_id: `FAKE-${task.task_id}-SNAPSHOT`, task_id: task.task_id, actor: "fake-preview",
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
  byId("project").addEventListener("change", () => {
    project = byId("project").value;
    if (!previewMode) return;
    selectedTask = null;
    selectedHistory = [];
    renderTaskSummary(null);
    renderHistory();
    void loadTasks().then(() => {
      notify(project === "skykeep"
        ? "SkyKeep repo is listed, but its project setup and task authority are not connected yet."
        : "SkyBuild repo selected. Local ledger records are fake and read-only.");
    }).catch(() => notify("Local preview task list is unavailable. No API or database connection was made.", true));
  });
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
