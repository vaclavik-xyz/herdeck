# Workspace names on deck tiles

The default heading follows the editable name in the connected application:

| Backend | `project` token fallback order |
| --- | --- |
| Herdr | Workspace display label, repository/worktree label, pane label |
| T3 | Project title, workspace-root basename, thread label |

Blank or whitespace-only names fall through. Surrounding whitespace is removed
from the display heading. Names never change pane/thread routing identity,
repository paths, or branch context. Two workspaces with identical names remain
separate tiles.

`project` is the default primary line on D200, the shared desktop/web renderer,
and the Elgato plugin. The desktop Project and Thread presets use `project` for
the project line. Existing custom line lists remain authoritative, including
explicit empty lists. The legacy `tile_fields` entry `repo` remains the visibility
switch for the default primary line on the shared renderer.

Explicit `repo` preserves the previous behavior: repository/worktree label for
Herdr, editable project title for T3. Explicit `workspace` exposes the raw Herdr
label and stays empty for T3. Token lists concatenate values with a separator;
they do not express a fallback chain.

Session titles remain independent. With an unset secondary line, a Herdr pane
title replaces `tab · branch`; T3 displays the thread title. An explicit
secondary line keeps its configured tokens.

## Rename delivery

Herdr's `session.snapshot.workspaces[].label` is mapped by the bridge to
`AgentState.workspace`. The bridge subscribes to `workspace.renamed` and rebuilds
the labels from the next snapshot; a periodic poll provides fallback delivery.
Reconnect reads the latest name directly from the snapshot. No protocol change
or additional workspace lookup is required.

`tests/test_project_names.py` covers fallback, Czech names, mixed backends,
duplicate display names, preserved session titles, explicit layouts, and rename
updates on both render paths. `tests/test_bridge.py` covers the rename wake
without refetching worktree data. Physical deployment verification must inspect
the actual render runtime, rendered tile bytes, and device delivery separately;
see [updating a deployment](updating-a-deployment.md).
