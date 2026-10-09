# Workbench status views

`source/` contains the copied SkyKeep status UI and the adjacent helper modules it needs. This isolated package supplies read-only preview routes at `/workbench/views/{view}` and `/workbench/api/state`. The dedicated `/workbench/tasks` page uses SkyBuild's existing authenticated task, journal, create, and defer/resume endpoints.

Use `http://127.0.0.1:8766/workbench` as the stable interactive preview URL. The local preview app enables source reload and browser refresh. The local-only Marshalls page controls the Dunsel development process; production routes do not expose process controls.
