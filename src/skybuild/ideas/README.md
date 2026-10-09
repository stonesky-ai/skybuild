# Ideas workbench page

This directory is the isolated landing area for the SkyKeep tools web app.

- `workbench_source/` contains the copied page package and its static page assets.
- The sibling helper scripts are kept beside it because the package loads them by relative path.
- `workbench.py` mounts the page in SkyBuild at `/ideas` and serves a local preview state.

The first integration is a UI preview. It does not start SkyKeep collectors, connect to the todo
service, or expose copied page write actions. Keep future changes for this embedded copy inside
this directory unless the project deliberately moves the integration elsewhere.
