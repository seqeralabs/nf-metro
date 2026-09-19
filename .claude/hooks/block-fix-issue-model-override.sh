#!/usr/bin/env bash
# PreToolUse hook (Agent tool): block a `model` override on a named
# fix-issue-* agent type, since its tier is already pinned in the agent
# definition's own frontmatter. See
# .claude/skills/fix-issue/references/agent-types.md for the full reasoning.
# The one documented exception is fix-issue-writer (lowering it to MID).
set -euo pipefail

input="$(cat)"

tool_name="$(printf '%s' "$input" | jq -r '.tool_name // ""')"
if [ "$tool_name" != "Agent" ]; then
  exit 0
fi

subagent_type="$(printf '%s' "$input" | jq -r '.tool_input.subagent_type // ""')"
model="$(printf '%s' "$input" | jq -r '.tool_input.model // ""')"

case "$subagent_type" in
  fix-issue-investigator|fix-issue-diagnostician|fix-issue-diagnostician-mid|fix-issue-reviewer|fix-issue-simplifier|fix-issue-verifier|fix-issue-gate-specialist|fix-issue-renderer|fix-issue-merge-assessor|fix-issue-visual-reviewer)
    if [ -n "$model" ]; then
      jq -n --arg t "$subagent_type" \
        '{hookSpecificOutput: {hookEventName: "PreToolUse", permissionDecision: "deny", permissionDecisionReason: ("Do not pass `model` when spawning `" + $t + "`. Its tier is already pinned in the agent definition'"'"'s own frontmatter; an explicit `model` silently overrides that pin. Omit the `model` parameter entirely. See .claude/skills/fix-issue/references/agent-types.md for the full reasoning and the one documented exception (lowering fix-issue-writer to MID).")}}'
      exit 0
    fi
    ;;
esac

exit 0
