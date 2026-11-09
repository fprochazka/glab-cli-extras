"""glab-cli-extras-cc-hook: the cases of the glab-pipeline and glab-discussion hook suites, and the hook's own
behaviour: what it denies, how a deny reads, its fallback, its failure policy and what it imports."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from glab_cli_extras.cc_hook import entry

FIXTURES = Path(__file__).parent / "fixtures" / "cc_hook"
BLOCK, ALLOW, PARSE = "block", "allow", "parse warning"

# The rows of the two shell suites, with the verdict they expected from a working bash-classify. Their degraded
# modes (an outdated or missing bash-classify) are gone: bash-classify is a locked dependency now.
PIPELINE_ROWS = [
    (BLOCK, "glab ci view 1000 2>&1 | head -50"),
    (BLOCK, "glab ci view --help"),
    (BLOCK, "glab ci view lint --web"),
    (BLOCK, "glab ci get --with-job-details -F json"),
    (BLOCK, "glab ci get -R group/project --with-job-details"),
    (BLOCK, "glab ci trace 2000 > /tmp/work/ci-job-2000.log 2>&1"),
    (BLOCK, "glab ci trace deploy-test -p 1000 -R group/project"),
    (BLOCK, "glab ci trace --pipeline-id 1000 tests-app"),
    (BLOCK, 'glab api "projects/:id/pipelines/1000/jobs?per_page=100"'),
    (BLOCK, 'glab api "projects/group%2Fproject/pipelines/1000"'),
    (BLOCK, 'GITLAB_HOST=gitlab.example.com glab api "projects/42/pipelines/1000"'),
    (BLOCK, "glab api projects/42/pipelines/1000 --hostname gitlab.example.com"),
    (BLOCK, 'glab api "projects/group%2Fproject/jobs/2000/trace" > /tmp/work/job.log'),
    (BLOCK, 'glab api "projects/42/jobs/2000/trace" 2>/dev/null | grep -i "test-env"'),
    (BLOCK, "glab api -X POST projects/group%2Fproject/ci/lint --input lint-payload.json"),
    (BLOCK, 'glab api --method POST projects/:id/ci/lint --field "content=x"'),
    (BLOCK, "sudo glab ci trace 2000"),
    (BLOCK, 'timeout 60 glab api "projects/:id/pipelines/1000/jobs"'),
    (BLOCK, 'bash -c "glab ci get -F json"'),
    (BLOCK, 'xargs -I{} glab api "projects/:id/jobs/{}/trace" < ids.txt'),
    (BLOCK, '/usr/bin/glab api "projects/:id/pipelines/1000"'),
    (BLOCK, "glab pipeline trace 2000"),
    (PARSE, 'eval "glab ci trace $(("'),
    (ALLOW, "glab ci list 2>&1 | head -10"),
    (ALLOW, "glab ci list --per-page 8"),
    (ALLOW, "glab ci status --compact"),
    (ALLOW, "glab ci status --pipeline-id 1000"),
    (ALLOW, "glab ci lint .gitlab-ci.yml 2>&1 | tail -5"),
    (ALLOW, 'glab api "projects/:id/pipelines?ref=feature/branch&per_page=1" | jq'),
    (ALLOW, "glab api 'projects/alice%2Fproject-test/pipelines?ref=master&per_page=1'"),
    (ALLOW, 'glab api "projects/group%2Fproject/merge_requests/123/pipelines"'),
    (ALLOW, 'glab api projects/:id/jobs/2000 2>/dev/null | jq -r ".name"'),
    (ALLOW, "glab-pipeline inspect --pipeline-id 1000"),
    (ALLOW, "glab-discussion read --dump"),
    (ALLOW, "git status"),
    (ALLOW, 'jq ".[] | {id}" /tmp/work/jobs.json'),
    (ALLOW, "glab auth status"),
    (ALLOW, ""),
    (ALLOW, 'echo "do not use glab ci get, use glab-pipeline inspect"'),
    (ALLOW, "printf '%s\\n' 'glab ci trace is blocked; run glab-pipeline inspect'"),
    (ALLOW, "grep -rn 'glab ci view\\|glab ci get\\|glab ci trace' docs/"),
    (ALLOW, "git commit -m \"Block 'glab ci trace' in PreToolUse hook\" && git log --oneline -1"),
    (ALLOW, 'for cmd in "glab ci list" "glab ci view 1000" "glab ci trace 2000"; do echo "$cmd"; done'),
    (ALLOW, "rg --no-messages -l -e 'glab ci (view|get|trace)' -g '*.jsonl' . | wc -l"),
]

DISCUSSION_ROWS = [
    (BLOCK, 'glab api "projects/:id/merge_requests/42/discussions" --paginate'),
    (BLOCK, "glab api projects/mygroup%2Fmyrepo/merge_requests/42/discussions"),
    (BLOCK, "glab api projects/123/merge_requests/5/discussions/abc/notes"),
    (BLOCK, "glab api projects/123/merge_requests/5/notes"),
    (BLOCK, "glab api projects/123/merge_requests/5/notes/99 -X DELETE"),
    (BLOCK, 'glab api "projects/123/merge_requests/5/discussions/abc" -X PUT -F resolved=true'),
    (BLOCK, "glab api 'projects/foo/merge_requests/1/discussions/X/notes/42' -X PUT"),
    (BLOCK, 'cd /tmp && glab api "projects/:id/merge_requests/42/discussions" > /tmp/out.json 2>&1; echo EXIT=$?'),
    (BLOCK, 'glab  api  "projects/x/merge_requests/9/discussions"'),
    (BLOCK, "glab api projects/123/merge_requests/5/draft_notes"),
    (BLOCK, 'glab api "projects/:id/merge_requests/5/draft_notes?per_page=100"'),
    (BLOCK, "glab api -X POST projects/123/merge_requests/5/draft_notes -f note=hi"),
    (BLOCK, "glab api projects/123/merge_requests/5/draft_notes/77 -X DELETE"),
    (BLOCK, 'glab api -X POST "projects/123/merge_requests/5/draft_notes/bulk_publish"'),
    (BLOCK, "glab api projects/123/merge_requests/5/notes?per_page=100"),
    (BLOCK, "glab api projects/123/merge_requests/5/notes/"),
    (BLOCK, "glab api -X POST projects/123/merge_requests/5/notes -f body=hi"),
    (BLOCK, "glab api projects/123/merge_requests/5/notes/99"),
    (BLOCK, "glab api projects/123/merge_requests/5/notes/99/ -X PUT -f body=x"),
    (BLOCK, 'glab api "projects/123/merge_requests/5/discussions?per_page=100"'),
    (BLOCK, "glab api projects/123/merge_requests/5/discussions/"),
    (BLOCK, "glab api -X POST projects/123/merge_requests/5/discussions -f body=hi"),
    (BLOCK, "glab api projects/123/merge_requests/5/discussions/6a9c1750b37d513a43987b574953fceb50b03ce7"),
    (BLOCK, "glab api -X POST projects/123/merge_requests/5/discussions/abc/notes -f body=hi"),
    (BLOCK, 'glab api "projects/123/merge_requests/5/discussions/abc/notes?per_page=100"'),
    (BLOCK, "glab api projects/123/merge_requests/5/discussions/abc/notes/42 -X DELETE"),
    (BLOCK, "glab api projects/123/merge_requests/5/draft_notes/77"),
    (BLOCK, "glab api projects/123/merge_requests/5/draft_notes/77 -X PUT -f note=x"),
    (BLOCK, "glab api -X PUT projects/123/merge_requests/5/draft_notes/77/publish"),
    (BLOCK, "glab api https://gitlab.example.com/api/v4/projects/group%2Fproj/merge_requests/5/notes"),
    (
        BLOCK,
        'glab api "https://gitlab.example.com/api/v4/projects/group%2Fproj/merge_requests/5/discussions?per_page=100"',
    ),
    (
        BLOCK,
        "glab api https://gitlab.example.com/api/v4/projects/group%2Fproj/merge_requests/5/draft_notes/bulk_publish"
        " -X POST",
    ),
    (BLOCK, 'glab api projects/123/merge_requests/5/notes|jq ".[].id"'),
    (ALLOW, "glab api projects/123/merge_requests/5/notes/99/award_emoji"),
    (ALLOW, "glab api projects/123/merge_requests/5/notes/99/award_emoji -X POST -f name=recycle"),
    (ALLOW, 'glab api "projects/:id/merge_requests/20/notes/1996757/award_emoji" -f name=thumbsup'),
    (ALLOW, "glab api projects/123/merge_requests/5/notes/99/award_emoji/7 -X DELETE"),
    (
        ALLOW,
        'glab api "https://gitlab.example.com/api/v4/projects/group%2Fproj/merge_requests/5/notes/99/award_emoji?per_page=100"',
    ),
    (ALLOW, "glab api projects/mygroup%2Fmyrepo/merge_requests/42/award_emoji"),
    (ALLOW, "glab api -X POST projects/123/merge_requests/5/award_emoji -f name=thumbsup"),
    (BLOCK, 'glab api -X POST projects/123/merge_requests/5/notes -f body="add an award_emoji instead"'),
    (ALLOW, "glab api projects/123/merge_requests/5/discussions/abc/notes/42/award_emoji"),
    (BLOCK, "glab mr view 42 --comments"),
    (BLOCK, "glab mr view 42 -R mygroup/myrepo --comments -F json"),
    (BLOCK, "cd /repo && glab mr view 42 --comments"),
    (BLOCK, "glab mr view 42 -c"),
    (BLOCK, "glab mr view -wc 42"),
    (BLOCK, "glab mr view 42 --unresolved"),
    (BLOCK, "glab mr view 42 --resolved"),
    (BLOCK, "glab mr note"),
    (BLOCK, "glab mr note 42"),
    (BLOCK, 'glab mr note 42 -m "hi"'),
    (BLOCK, 'glab mr note 42 --message "hi"'),
    (BLOCK, 'glab mr note 42 -R mygroup/myrepo -m "x"'),
    (BLOCK, 'glab mr note my-branch-name -m "text"'),
    (BLOCK, "glab mr note resolve"),
    (BLOCK, "glab mr note resolve DISC_ID"),
    (BLOCK, "glab mr note reopen DISC_ID"),
    (ALLOW, "glab mr note list"),
    (ALLOW, "glab mr note list 42"),
    (ALLOW, "glab mr note list -R mygroup/myrepo"),
    (ALLOW, "glab  mr  note  list"),
    (BLOCK, "glab mr note create 42 -m x"),
    (BLOCK, "glab mr note update 42 NOTE_ID -m x"),
    (BLOCK, "glab mr note delete 42 NOTE_ID"),
    (BLOCK, "sudo glab mr note 42 -m hi"),
    (BLOCK, 'timeout 60 glab api "projects/:id/merge_requests/42/discussions"'),
    (BLOCK, 'bash -c "glab mr note 42 -m hi"'),
    (BLOCK, 'xargs -I{} glab api "projects/{}/merge_requests/42/notes" < ids.txt'),
    (BLOCK, '/usr/bin/glab api "projects/:id/merge_requests/42/discussions"'),
    (PARSE, 'eval "glab mr note $(("'),
    (ALLOW, "glab mr view 42"),
    (ALLOW, "glab mr view 42 -R mygroup/myrepo -F json"),
    (ALLOW, "glab mr diff 42"),
    (ALLOW, "glab mr list"),
    (ALLOW, "glab mr create"),
    (ALLOW, "glab mr approve 42"),
    (ALLOW, "glab mr todo 42"),
    (ALLOW, "glab api projects/mygroup%2Fmyrepo/merge_requests/42"),
    (ALLOW, 'glab api "projects/mygroup%2Fmyrepo"'),
    (ALLOW, 'glab api "projects?search=myrepo"'),
    (ALLOW, 'glab api "projects?search=x" --hostname gitlab.example.com'),
    (ALLOW, 'jq ".[] | {id}" /tmp/out.json'),
    (ALLOW, "git status"),
    (ALLOW, "glab-discussion read"),
    (ALLOW, 'glab-discussion write --body "x"'),
    (ALLOW, 'glab-discussion write --draft --body "x"'),
    (ALLOW, "glab-discussion drafts publish --verdict reviewed"),
    (ALLOW, "glab-discussion drafts delete --force"),
    (ALLOW, "glab auth status"),
    (ALLOW, ""),
    (ALLOW, 'echo "use glab mr note 42 -m x instead of the API"'),
    (ALLOW, 'grep -rn "glab mr view --comments" ~/.claude/'),
    (ALLOW, 'echo "glab api projects/1/merge_requests/2/draft_notes is blocked"'),
]


def payload(command: Any, *, tool: str = "Bash", event: str = "PreToolUse") -> dict[str, Any]:
    tool_input = {} if command is None else {"command": command}
    return {
        "session_id": "0b1c2d3e-4f50-6172-8394-a5b6c7d8e9f0",
        "transcript_path": "/home/user/.claude/projects/-tmp-project/0b1c2d3e.jsonl",
        "cwd": "/tmp/project",
        "permission_mode": "default",
        "hook_event_name": event,
        "tool_name": tool,
        "tool_input": tool_input,
        "tool_use_id": "toolu_01",
    }


def run_handler(data: dict[str, Any]) -> tuple[int, str, str]:
    from cc_blackbox.hooks import handle_hook

    out, err = io.StringIO(), io.StringIO()
    code = handle_hook(
        entry.handle, on_error="pass", name=entry.NAME, stdin=io.StringIO(json.dumps(data)), stdout=out, stderr=err
    )
    return code, out.getvalue(), err.getvalue()


def verdict(command: str) -> tuple[str, str]:
    """(block or allow, the deny reason) for a Bash command, through the handler in process."""
    code, out, _ = run_handler(payload(command))
    assert code == 0
    if not out:
        return ALLOW, ""
    reply = json.loads(out)["hookSpecificOutput"]
    assert reply["hookEventName"] == "PreToolUse" and reply["permissionDecision"] == "deny"
    return BLOCK, str(reply["permissionDecisionReason"])


def check(expected: str, command: str, skill: str) -> None:
    got, reason = verdict(command)
    assert got == (ALLOW if expected == ALLOW else BLOCK), command
    if expected == BLOCK:
        assert "(matched rule: " in reason and "Note:" not in reason, reason
        assert f"`{skill}`" in reason
        assert "uv tool install glab-cli-extras" in reason
    if expected == PARSE:
        assert "could not fully parse" in reason


@pytest.mark.parametrize(("expected", "command"), PIPELINE_ROWS)
def test_pipeline_rows(expected: str, command: str) -> None:
    check(expected, command, "glab:glab-pipeline")


@pytest.mark.parametrize(("expected", "command"), DISCUSSION_ROWS)
def test_discussion_rows(expected: str, command: str) -> None:
    check(expected, command, "glab:glab-discussion")


CASE_FILES = sorted(FIXTURES.glob("*/*/*.sh"))


@pytest.mark.parametrize("path", CASE_FILES, ids=lambda p: "/".join(p.parts[-3:]))
def test_case_files(path: Path) -> None:
    rule_set, expected = path.parts[-3], path.parts[-2]
    check(expected, path.read_text(encoding="utf-8"), f"glab:glab-{rule_set}")


def test_every_case_file_of_both_suites_is_here() -> None:
    counts = {(p.parts[-3], p.parts[-2]): 0 for p in CASE_FILES}
    for p in CASE_FILES:
        counts[(p.parts[-3], p.parts[-2])] += 1
    assert counts == {
        ("discussion", "allow"): 9,
        ("discussion", "block"): 6,
        ("pipeline", "allow"): 5,
        ("pipeline", "block"): 10,
    }


def test_a_command_that_invokes_both_kinds_names_both_skills_and_rules() -> None:
    got, reason = verdict("glab ci get -F json && glab mr note 42 -m hi")
    assert got == BLOCK
    assert "`glab:glab-pipeline`" in reason and "`glab:glab-discussion`" in reason
    assert "(matched rule: ci-get)" in reason and "(matched rule: mr-note)" in reason


def test_the_parse_warning_fallback_still_allows_what_no_pattern_matches() -> None:
    assert verdict('eval "glab ci list $(("') == (ALLOW, "")


def test_a_failing_bash_classify_falls_back_to_the_text_patterns(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(command: str) -> Any:
        raise RuntimeError("no parser")

    monkeypatch.setattr(entry, "_match", broken)
    got, reason = verdict("glab ci trace 2000")
    assert got == BLOCK
    assert "Note: bash-classify failed on this command (RuntimeError: no parser)" in reason
    assert verdict("glab ci list") == (ALLOW, "")


def test_a_command_without_glab_never_loads_bash_classify(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(command: str) -> Any:
        raise AssertionError("bash-classify was asked")

    monkeypatch.setattr(entry, "_match", fail)
    assert verdict("git status && ls -la") == (ALLOW, "")


@pytest.mark.parametrize(
    "data",
    [
        payload(None),
        payload(42),
        payload("   "),
        payload("glab ci get", tool="Read"),
        payload("glab ci get", event="PostToolUse"),
        {"hook_event_name": "SessionStart", "source": "startup"},
    ],
    ids=["no command", "not a string", "blank", "another tool", "another event", "another event without a tool"],
)
def test_what_is_not_a_bash_call_has_no_opinion(data: dict[str, Any]) -> None:
    assert run_handler(data) == (0, "", "")


def test_a_crash_prints_nothing_on_stdout_and_exits_1(monkeypatch: pytest.MonkeyPatch) -> None:
    def crash(command: str) -> str | None:
        raise RuntimeError("boom")

    monkeypatch.setattr(entry, "check_command", crash)
    code, out, err = run_handler(payload("glab ci get"))
    assert (code, out) == (1, "")
    assert err.startswith("glab-cli-extras-cc-hook hook failed: RuntimeError: boom")


HEAVY = (
    "typer",
    "rich",
    "glab_cli_extras.cli",
    "glab_cli_extras.gitlab",
    "glab_cli_extras.daemon",
    "glab_cli_extras.watch",
    "glab_cli_extras.status",
    "asyncio",
)
PROBE = """
import io, json, sys
sys.stdin = io.StringIO(sys.argv[1])
from glab_cli_extras.cc_hook import entry
try:
    entry.main()
except SystemExit as exc:
    code = exc.code
sys.stderr.write(json.dumps({"code": code, "modules": sorted(sys.modules)}))
"""


def run_entry(command: str) -> tuple[dict[str, Any], str]:
    result = subprocess.run(
        [sys.executable, "-c", PROBE, json.dumps(payload(command))],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    return json.loads(result.stderr), result.stdout


@pytest.mark.parametrize("command", ["glab ci get -F json", "glab mr list", "git status"])
def test_the_hook_path_imports_no_heavy_module(command: str) -> None:
    probe, _ = run_entry(command)
    heavy = [m for m in probe["modules"] if any(m == h or m.startswith(h + ".") for h in HEAVY)]
    assert heavy == []
    assert ("bash_classify" in probe["modules"]) == ("glab" in command)


def test_the_entry_point_denies_and_allows_in_a_subprocess() -> None:
    probe, out = run_entry("glab api projects/42/pipelines/1000")
    assert probe["code"] == 0
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"
    probe, out = run_entry("glab api 'projects/42/pipelines?ref=main'")
    assert (probe["code"], out) == (0, "")
