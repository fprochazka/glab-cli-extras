"""Rules the plugin's skills must keep, checked on their text."""

from __future__ import annotations

import re
from pathlib import Path

from glab_cli_extras.cc_hook.entry import RULE_SETS

PLUGIN = Path(__file__).resolve().parents[1] / "coding-agent-plugins" / "claude-code"
CODE_BLOCK = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)


def test_no_skill_example_passes_events_stream() -> None:
    """A watcher started with --events-stream=stdout under an agent talks to an output file nobody reads; the
    default reaches the session. No example may teach the option."""
    offenders = [
        f"{path.relative_to(PLUGIN)}: {line.strip()}"
        for path in sorted(PLUGIN.rglob("*.md"))
        for block in CODE_BLOCK.findall(path.read_text(encoding="utf-8"))
        for line in block.splitlines()
        if "--events-stream" in line
    ]
    assert offenders == []


SKILL_REF = re.compile(r"`/?glab:([a-z-]+)`")
SKILL_NAMES = sorted(path.parent.name for path in (PLUGIN / "skills").glob("*/SKILL.md"))
FRONT_MATTER_NAME = re.compile(r"\A---\n(?:.*\n)*?name: (.+)\n(?:.*\n)*?---\n")


def test_every_glab_skill_the_plugin_and_the_hook_name_exists() -> None:
    """The Bash guard's deny reasons and the skills send the agent to a skill by name; a removed or renamed skill
    leaves the agent loading nothing."""
    sources = [*sorted(PLUGIN.rglob("*.md")), *sorted((PLUGIN.parents[1] / "src" / "glab_cli_extras").rglob("*.py"))]
    named = {name for path in sources for name in SKILL_REF.findall(path.read_text(encoding="utf-8"))}
    assert named
    assert sorted(name for name in named if not (PLUGIN / "skills" / name / "SKILL.md").is_file()) == []


def test_every_deny_reason_of_the_hook_names_an_existing_skill() -> None:
    reasons = {rule_set.name: SKILL_REF.findall(rule_set.reason) for rule_set in RULE_SETS}
    assert reasons == {"pipeline": ["glab-pipeline"], "discussion": ["glab-discussion"]}
    assert all((PLUGIN / "skills" / name / "SKILL.md").is_file() for names in reasons.values() for name in names)


def test_every_skill_and_command_is_named_with_the_plugin_prefix() -> None:
    """Claude Code turns a front matter `name` without a colon into a bare alias, so `name: glab-watch` would also
    answer to `/glab-watch` and show `/glab:glab-watch (glab-watch)` in the slash menu. `glab:<name>` creates no
    alias and keeps the name the skill is invoked by."""
    files = sorted([*(PLUGIN / "skills").glob("*/SKILL.md"), *(PLUGIN / "commands").glob("*.md")])
    names = {}
    for path in files:
        match = FRONT_MATTER_NAME.match(path.read_text(encoding="utf-8"))
        names[str(path.relative_to(PLUGIN))] = match.group(1).strip() if match else None
    expected = {
        str(path.relative_to(PLUGIN)): f"glab:{path.parent.name if path.name == 'SKILL.md' else path.stem}"
        for path in files
    }
    assert SKILL_NAMES
    assert names == expected


_ANY_SKILL = "|".join(re.escape(name) for name in SKILL_NAMES)
BARE_REFERENCES = (
    re.compile(rf"(?<![\w:/-])`?(?:{_ANY_SKILL})`?\s+skills?\b", re.IGNORECASE),
    re.compile(rf"(?<![\w./:>-])/(?:{_ANY_SKILL})(?![\w:-])"),
    re.compile(rf"\bSkill\((?:{_ANY_SKILL})\)"),
)


def test_no_text_sends_the_agent_to_a_skill_or_command_by_its_bare_name() -> None:
    """The skills and commands have no bare alias, so "load the glab-watch skill", `/overview` or `Skill(glab)` find
    nothing; every reference meant for invocation uses `glab:<name>` or `/glab:<name>`. The CLI's own commands, such
    as `glab-watch`, are executables and stay as they are."""
    root = PLUGIN.parents[1]
    cc_hook = root / "src" / "glab_cli_extras" / "cc_hook"
    sources = [
        *sorted(PLUGIN.rglob("*.md")),
        *sorted((PLUGIN / "hooks").glob("*.sh")),
        root / "README.md",
        *sorted(cc_hook.rglob("*.py")),
        *sorted(cc_hook.rglob("*.yaml")),
    ]
    offenders = [
        f"{path.relative_to(root)}:{number}: {match.group(0)}"
        for path in sources
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        for pattern in BARE_REFERENCES
        for match in pattern.finditer(line)
    ]
    assert offenders == []


def test_the_glab_skill_covers_pagination_bodies_in_files_and_the_extras_commands() -> None:
    """The glab skill exists for the mistakes glab's own --help does not prevent; each needs its section."""
    text = (PLUGIN / "skills" / "glab" / "SKILL.md").read_text(encoding="utf-8")
    assert re.findall(r"^## .+$", text, re.MULTILINE) == [
        "## Pagination",
        "## Long bodies go into a file",
        "## Use the glab-cli-extras commands for their jobs",
    ]
    for command in (
        "glab-pipeline inspect",
        "glab-discussion",
        "glab-watch",
        "glab-mr-status",
        "--paginate",
        "--input",
    ):
        assert command in text
