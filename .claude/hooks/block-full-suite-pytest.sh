#!/usr/bin/env bash
# PreToolUse hook (Bash tool): refuse a local full test suite. CI owns the full
# suite; a local run per branch or per worker buys no signal and costs an hour
# of CPU. The command is allowed when it names a test file, uses -k or --lf,
# or carries the `full-suite-ok` marker (the one late, deliberate run).
# See .claude/skills/fix-issue/references/writer-steps.md.
set -euo pipefail

input="$(cat)"

tool_name="$(printf '%s' "$input" | jq -r '.tool_name // ""')"
if [ "$tool_name" != "Bash" ]; then
  exit 0
fi

cmd="$(printf '%s' "$input" | jq -r '.tool_input.command // ""')"

case "$cmd" in
  *pytest*) ;;
  *) exit 0 ;;
esac

if printf '%s' "$cmd" | grep -q 'full-suite-ok'; then
  exit 0
fi
if printf '%s' "$cmd" | grep -Eq 'tests?/[A-Za-z0-9_/.-]*test_[A-Za-z0-9_]+\.py|(^|[[:space:]])-k[[:space:]=]|--lf([[:space:]]|$)|--last-failed|--self-test|--collect-only|--version'; then
  exit 0
fi

jq -n '{hookSpecificOutput: {hookEventName: "PreToolUse", permissionDecision: "deny", permissionDecisionReason: "This looks like a local full test suite. CI owns the full suite; run the targeted test file (tests/test_<module>.py), -k <expr> or --lf instead. For the one deliberate late run that writer-steps.md allows, append `# full-suite-ok` to the command."}}'
exit 0
