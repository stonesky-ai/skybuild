# Petri metadata limits

Caller-supplied task metadata has a 16 KiB JSON limit. Public task creation and
edits reject the reserved `_skybuild_workflow` and `_skybuild_completion` keys.

Internally managed task metadata has a separate 64 KiB aggregate JSON limit.
This total includes caller data and the reserved workflow and acceptance records.
Each nested `TaskToken` and `ValidationResult` retains its 16 KiB record limit.
The shared Store validator checks the caller portion, aggregate size, reserved
object shapes, workflow generation, and versioned Petri token.

Initialization, workflow progress, and ordinary task edits use the same validator.
An edit that inherits valid managed evidence does not apply the caller limit to
that evidence. A confirmed validation failure can coexist with valid legacy user
metadata. Large logs remain external artifacts; task records hold references.

Journal `event_facts` preserves the complete bounded incoming result when the
current token uses a compact failure record. Its separate 64 KiB limit remains
unchanged. This change does not authorize callers to write managed evidence.
