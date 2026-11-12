"""`glab-cli-extras-cc-hook`: the Claude Code PreToolUse guard for Bash commands that bypass these commands.

It denies inspecting pipelines and job logs with `glab ci` or the raw API, and reading or writing MR discussions with
`glab api` or `glab mr note`, and names the command and the skill to use instead.

Claude Code starts the hook on every Bash tool call, so its start-up time is the cost. This module imports
`cc_blackbox.hooks` and, only for a command that mentions glab, bash-classify. It never imports the CLI, the GitLab
client, the daemon, typer or rich; a test enforces that.

The verdict comes from bash-classify, which parses the command and reports which rule shapes it invokes. Text that
only names a blocked command (a heredoc body, an echo argument, a commit message) is not an invocation. When
bash-classify cannot fully parse the command, or fails, the hook falls back to text patterns, which also fire on such
text, and says so in the deny reason.
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cc_blackbox.hooks import HookInput, HookReply

NAME = "glab-cli-extras-cc-hook"
RULES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rules")


@dataclass(frozen=True)
class RuleSet:
    name: str
    reason: str
    fallback: tuple[re.Pattern[str], ...]


_SEGMENT = r"""[^/?"'`\s]+"""
_MR_DISCUSSIONS_PATH = (
    rf"merge_requests/{_SEGMENT}/(notes(/[0-9]+)?|discussions(/{_SEGMENT}(/notes(/[0-9]+)?)?)?"
    r"|draft_notes(/bulk_publish|/[0-9]+(/publish)?)?)/?([?\"'`\s;|&<>)]|$)"
)
"""The `any_arg_matches` pattern of the mr-discussions-api rule, adapted to raw command text: a path segment also
stops at a quote, a backtick or whitespace, and the path ends at one of those or a shell operator. Change both
together."""

RULE_SETS: tuple[RuleSet, ...] = (
    RuleSet(
        "pipeline",
        "Direct `glab` pipeline inspection is blocked (matched: `glab ci view|get|trace`, or raw"
        " `glab api projects/.../pipelines|jobs/.../trace`, or a GET of `projects/.../ci/lint`). Use"
        " `glab-pipeline inspect` instead: it dumps the full pipeline state (pipeline.json, jobs.json, per-job logs,"
        " lint, test report, downstream) and prints a problem-driven summary; `--with-merged-ci-config` adds a ref's"
        " lint result. Linting a file stays allowed: `glab ci lint <file>`, or POSTing its content to"
        " `projects/.../ci/lint` with `glab api -X POST`, `--input` or `-f content=...`. To wait for a pipeline, run"
        " `glab-watch pipeline` in the background. Install with"
        " `uv tool install glab-cli-extras`. Load the `glab:glab-pipeline` skill for usage.",
        tuple(
            re.compile(p, re.MULTILINE)
            for p in (
                r"glab[ \t]+ci[ \t]+(view|get|trace)\b",
                r"glab[ \t]+api\b[^|;&\n]*projects/\S+/pipelines/",
                r"glab[ \t]+api\b[^|;&\n]*projects/\S+/jobs/\S+/trace",
                # The ci-lint-api rule's except_option: a segment that names a method or a field sends content.
                r"glab[ \t]+api\b(?![^|;&\n]*[ \t](-X|--method|--input|-f|-F|--field|--raw-field)([ \t=]|$))"
                r"[^|;&\n]*projects/\S+/ci/lint",
            )
        ),
    ),
    RuleSet(
        "discussion",
        "Reading or writing MR discussions/notes via `glab` is blocked (matched: `glab api"
        " .../discussions|notes|draft_notes`, `glab mr view --comments`, or `glab mr note`). Use the `glab-discussion`"
        " CLI instead: `glab-discussion read|write|diff|resolve|edit|delete|drafts`. Install with"
        " `uv tool install glab-cli-extras`. Run `glab-discussion --help` for usage, or load the"
        " `glab:glab-discussion` skill.",
        tuple(
            re.compile(p, re.MULTILINE)
            for p in (
                rf"glab[ \t]+api\b.*{_MR_DISCUSSIONS_PATH}",
                r"glab[ \t]+mr[ \t]+view\b.*--comments\b",
                r"glab[ \t]+mr[ \t]+note\b",
            )
        ),
    ),
)

PARSE_WARNING_NOTE = (
    "bash-classify could not fully parse this command, so a text pattern was used instead; the pattern can misfire"
    " on heredocs or quoted text that merely mentions these commands."
)


def _fallback(command: str, note: str) -> str | None:
    reasons = [rs.reason for rs in RULE_SETS if any(p.search(command) for p in rs.fallback)]
    if not reasons:
        return None
    return "\n\n".join(reasons) + f"\n\nNote: {note}"


def _match(command: str) -> tuple[dict[str, list[str]], Sequence[str]]:
    """The names of the matched rules per rule set, and bash-classify's parse warnings."""
    from bash_classify import load_rules, match_expression

    owner: dict[str, str] = {}
    rules = []
    for rule_set in RULE_SETS:
        for rule in load_rules(os.path.join(RULES_DIR, f"{rule_set.name}.yaml")):
            owner[rule.name] = rule_set.name
            rules.append(rule)
    result = match_expression(command, rules)
    matched: dict[str, list[str]] = {}
    for match in result.matches:
        names = matched.setdefault(owner.get(match.rule, ""), [])
        if match.rule not in names:
            names.append(match.rule)
    return matched, result.parse_warnings


def check_command(command: str) -> str | None:
    """The deny reason for a Bash command, or None to have no opinion."""
    # Every rule is about glab, and a command that never says "glab" cannot invoke it, so most Bash calls are
    # answered without loading bash-classify at all.
    if "glab" not in command:
        return None
    try:
        matched, warnings = _match(command)
    except Exception as exc:
        return _fallback(
            command,
            f"bash-classify failed on this command ({type(exc).__name__}: {exc}), so a text pattern was used instead;"
            " tell the user, the hook or its bash-classify version may be broken.",
        )
    if warnings:
        return _fallback(command, PARSE_WARNING_NOTE)
    reasons = [f"{rs.reason} (matched rule: {', '.join(matched[rs.name])})" for rs in RULE_SETS if rs.name in matched]
    return "\n\n".join(reasons) or None


def handle(event: HookInput) -> HookReply | None:
    from cc_blackbox.hooks import PreToolUseInput, PreToolUseReply

    if not isinstance(event, PreToolUseInput) or event.tool_name != "Bash":
        return None
    command = event.tool_input.get("command")
    if not isinstance(command, str) or not command.strip():
        return None
    reason = check_command(command)
    return PreToolUseReply.deny(reason) if reason else None


def main() -> None:
    from cc_blackbox.hooks import run_hook

    run_hook(handle, on_error="pass", name=NAME)
