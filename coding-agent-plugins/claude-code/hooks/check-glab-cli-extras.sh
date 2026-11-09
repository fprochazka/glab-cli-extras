#!/bin/sh
# SessionStart check: is glab-cli-extras installed, recent enough for this plugin, and the owner of glab-pipeline and
# glab-discussion? Prints nothing when all is well, and otherwise context that asks the agent to tell the user and
# offer the command that fixes it. Always exits 0.

plugin_root=${CLAUDE_PLUGIN_ROOT:-$(dirname "$0")/..}
problems=""

add() {
  problems="${problems:+$problems }$1"
}

# major.minor of the first x.y.z in the input
minor_of() {
  sed -n 's/[^0-9]*\([0-9][0-9]*\)\.\([0-9][0-9]*\)\..*/\1 \2/p' | head -n 1
}

if ! command -v glab-watch >/dev/null 2>&1; then
  add "glab-cli-extras is not installed, so the commands the glab plugin's skills use (glab-watch, glab-mr-status, glab-pipeline, glab-discussion) and its Bash guard are missing. Tell the user and offer to run: uv tool install glab-cli-extras"
else
  wanted=$(sed -n 's/.*"version"[^"]*"\([^"]*\)".*/\1/p' "$plugin_root/.claude-plugin/plugin.json" 2>/dev/null | head -n 1)
  installed=$(glab-watch --version 2>/dev/null | head -n 1)
  # Split on purpose: both values are digits only, and an empty one leaves fewer than four words.
  # shellcheck disable=SC2046
  set -- $(printf '%s\n' "$wanted" | minor_of) $(printf '%s\n' "$installed" | minor_of)
  if [ $# -eq 4 ] && { [ "$3" -lt "$1" ] || { [ "$3" -eq "$1" ] && [ "$4" -lt "$2" ]; }; }; then
    add "The installed glab-cli-extras ($(printf '%s' "$installed" | tr -cd '0-9A-Za-z. -')) is older than the glab plugin ($(printf '%s' "$wanted" | tr -cd '0-9A-Za-z.-')), so the skills may name options it lacks. Tell the user and offer to run: uv tool upgrade glab-cli-extras"
  fi
  # A version number can agree while the install predates a command, for example an editable install whose entry
  # points were created by an older release.
  missing=""
  for command in glab-mr-status glab-cli-extras-cc-hook; do
    command -v "$command" >/dev/null 2>&1 || missing="${missing:+$missing, }$command"
  done
  if [ -n "$missing" ]; then
    add "The installed glab-cli-extras lacks $missing, which the glab plugin uses. Tell the user and offer to run: uv tool upgrade --reinstall glab-cli-extras"
  fi
  # glab-cli-extras makes both print "glab-cli-extras <version>"; the standalone packages do not.
  shadowed=""
  for command in glab-pipeline glab-discussion; do
    if command -v "$command" >/dev/null 2>&1; then
      case "$("$command" --version 2>/dev/null | head -n 1)" in
        "glab-cli-extras "*) ;;
        *) shadowed="${shadowed:+$shadowed }$command" ;;
      esac
    fi
  done
  if [ -n "$shadowed" ]; then
    add "The $shadowed on PATH is the deprecated standalone package, not glab-cli-extras, so it lacks what the skills describe. Tell the user and offer to run: uv tool uninstall $shadowed && uv tool install --force glab-cli-extras"
  fi
fi

[ -n "$problems" ] || exit 0
printf '{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"%s"}}\n' "glab plugin: $problems"
exit 0
