# Workbench status views

`source/` contains the copied SkyKeep status UI and the adjacent helper modules it needs. This isolated package supplies read-only preview routes at `/workbench/views/{view}` and `/workbench/api/state`; the task list on `/workbench` continues to use SkyBuild's authenticated task API.

Use `http://127.0.0.1:8766/workbench` as the stable interactive preview URL. The local preview app enables source reload and browser refresh.
