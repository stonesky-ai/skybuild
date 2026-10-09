(() => {
  const byId = (id) => document.getElementById(id);
  const localControls = document.documentElement.dataset.marshallsControls === "true";
  const controls = ["enable", "disable", "start", "graceful", "kill"]
    .map((name) => byId(`dunsel-${name}`));

  function notice(message, error = false) {
    const node = byId("dunsel-notice");
    node.textContent = message;
    node.dataset.error = String(error);
  }

  function setState(name, label) {
    const node = byId("dunsel-state");
    node.dataset.state = name;
    node.textContent = label;
  }

  function render(snapshot) {
    byId("dunsel-enabled").textContent = snapshot.enabled ? "Yes" : "No";
    byId("dunsel-pid").textContent = snapshot.running ? String(snapshot.pid) : "Stopped";
    byId("dunsel-last-seen").textContent = snapshot.last_seen || "No log file";
    byId("dunsel-observed").textContent = snapshot.observed_at || "Unknown";
    byId("dunsel-processes").textContent = snapshot.processes?.length
      ? snapshot.processes.map((row) => `PID ${row.pid} · PPID ${row.ppid} · ${row.state}\n${row.command}`).join("\n\n")
      : "No Dunsel process found.";
    byId("dunsel-top").textContent = snapshot.top_line || "No matching `top` row (process may have exited).";
    byId("dunsel-log").textContent = snapshot.last_line || "No log line yet.";

    byId("dunsel-enable").disabled = !localControls || snapshot.enabled;
    byId("dunsel-disable").disabled = !localControls || !snapshot.enabled;
    byId("dunsel-start").disabled = !localControls || !snapshot.enabled || snapshot.running;
    byId("dunsel-graceful").disabled = !localControls || !snapshot.running || snapshot.exit_requested;
    byId("dunsel-kill").disabled = !localControls || !snapshot.running;
    if (!localControls) setState("disabled", "Local preview only");
    else if (!snapshot.enabled) setState("disabled", "Disabled");
    else if (snapshot.running) setState("running", "Running");
    else setState("stopped", "Stopped");
  }

  async function refresh() {
    if (!localControls) {
      byId("preview-boundary").hidden = false;
      controls.forEach((button) => { button.disabled = true; });
      byId("dunsel-refresh").disabled = true;
      setState("disabled", "Local preview only");
      notice("Open the loopback Workbench preview to control this local process.");
      return;
    }
    byId("dunsel-refresh").disabled = true;
    try {
      const response = await fetch("/workbench/api/marshalls/dunsel", {
        credentials: "same-origin", cache: "no-store", redirect: "error",
      });
      if (!response.ok) throw new Error(`Status request failed (${response.status})`);
      render(await response.json());
      notice("Status refreshed.");
    } catch (error) {
      setState("error", "Unavailable");
      notice(error.message || "Dunsel status is unavailable.", true);
    } finally {
      byId("dunsel-refresh").disabled = !localControls;
    }
  }

  async function act(action) {
    const button = byId(`dunsel-${action}`);
    button.disabled = true;
    notice("Applying control…");
    try {
      const response = await fetch(`/workbench/api/marshalls/dunsel/${action}`, {
        method: "POST", credentials: "same-origin", cache: "no-store", redirect: "error",
      });
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail || `Control failed (${response.status})`);
      notice(body.message || `${action} request completed.`);
    } catch (error) {
      notice(error.message || "Dunsel control failed.", true);
    } finally {
      await refresh();
    }
  }

  if (localControls) {
    byId("dunsel-enable").addEventListener("click", () => act("enable"));
    byId("dunsel-disable").addEventListener("click", () => act("disable"));
    byId("dunsel-start").addEventListener("click", () => act("start"));
    byId("dunsel-graceful").addEventListener("click", () => act("graceful"));
    byId("dunsel-kill").addEventListener("click", () => act("kill"));
    byId("dunsel-refresh").addEventListener("click", refresh);
    refresh();
    window.setInterval(refresh, 60_000);
  } else {
    refresh();
  }
})();
