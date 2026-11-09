#!/bin/sh
# PreToolUse guard: runs glab-cli-extras-cc-hook and always exits 0.
#
# The hook command decides; this script only makes sure a broken or missing install never blocks a Bash call.
# - glab-cli-extras-cc-hook not on PATH: no opinion. The SessionStart check tells the user how to install it.
# - It exits 0: its stdout (a deny, or nothing) goes to Claude Code.
# - It exits non-zero (a crash exits 1): no opinion, nothing printed. An exit 2 from a PreToolUse hook would block
#   the call, and an exit 1 would show a hook error notice on every Bash call. Its stderr still reaches
#   `claude --debug`.
# It is a shell script because it must run when the Python command is missing.

command -v glab-cli-extras-cc-hook >/dev/null 2>&1 || exit 0
if out=$(glab-cli-extras-cc-hook); then
  [ -n "$out" ] && printf '%s\n' "$out"
fi
exit 0
