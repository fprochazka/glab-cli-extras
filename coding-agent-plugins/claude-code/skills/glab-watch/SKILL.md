---
name: glab-watch
description: >-
  This skill should be used when the session has to wait for something on GitLab or react to it as it
  happens: "watch this MR", "wait for the pipeline", "tell me when CI finishes", "let me know when someone
  comments", "watch the branch", "watch every MR of the project", "babysit the MR", or when a
  `<glab-cli-extras-watcher>` message arrives. Covers starting glab-watch in the background, its startup
  header, event filters, reacting to its messages, stopping it, and the files it writes.
trigger-keywords: glab-watch, watch mr, wait for pipeline, wait for ci, glab-cli-extras-watcher
allowed-tools: ["Bash(glab-watch --help)", "Bash(glab-watch daemon status:*)", "Bash(glab-watch daemon logs:*)"]
---

# glab-watch

`glab-watch` watches a merge request, a pipeline, every MR of a project, or a branch, and reports each change as an event. Under Claude Code, the events arrive in this conversation as messages, so the session learns about a push, a comment or a finished pipeline without asking. It comes with glab-cli-extras (`uv tool install glab-cli-extras`).

## Watch or read once

- "What is it now?": run `glab-mr-status` (see the mr-status skill) or `glab-pipeline inspect`. One read, one answer.
- "Tell me when it changes": start a watcher. **Never poll** with `sleep`, a loop, or repeated reads: a watcher costs a fraction of the API budget and reports the change within seconds of the next poll.

## Starting a watcher

Start every watcher with the Bash tool and `run_in_background: true`, one watcher per resource:

```bash
glab-watch mr https://gitlab.example.com/group/project/-/merge_requests/123
glab-watch pipeline https://gitlab.example.com/group/project/-/pipelines/4567 --until=finished
glab-watch mrs group/project --events=status
glab-watch branch group/project main
```

- **Refs**: an MR or pipeline URL, `group/project!123`, `!123` or `123` with the repo of the current directory. Use URLs when the set spans repos or hosts: outside a checkout, glab falls back to its default host.
- **`--until=<kind>`** ends the watch after the first event of that kind, for example `--until=finished` to wait for one pipeline. **`--timeout=<duration>`** ends it that long after the start.
- **`--idle-after=<duration>`** ends it when nothing was reported for that long (`watch_ended` reason `idle`).
- **`--quiet-after=<duration>`** (`mr`, repeatable) reports `quiet` once per head when the head pipeline succeeded on it and nothing else was reported for that long. **`--stuck-after=<duration>`** (`mr`, `pipeline`) reports `pipeline_stuck` once per pipeline still `running` or `pending` that long. A babysitting run that waits for review uses all of them:

  ```bash
  glab-watch mr <url> --quiet-after=2m --quiet-after=15m --stuck-after=30m --idle-after=3h
  ```

- **Start watchers from the main session.** A watcher started from a subagent messages the main session anyway; Claude Code has no route to a subagent.

### Leave `--events-stream` at its default

Start every watcher **without** `--events-stream`. The default, `auto`, sends each event to this session as a message when the watcher runs under a session that accepts them, so you learn about a change when it happens, without checking. When it cannot, it falls back to stdout and says why.

With `--events-stream=stdout`, the events go only to the background task's output and to `events.jsonl`. Nothing tells you when one arrives, so you would have to read those files yourself, again and again, to notice a change. `--events-stream=claude-message` gains nothing over `auto`, and fails at start where `auto` would fall back. The option is there for scripts and terminals.

## The startup header

Before anything else, read the header in the background task's output. Each line starts with `glab-watch: `:

- `stream:` confirms the mode. `claude-message` means the events arrive as messages. A `stream fallback:` line means they do not: read its reason, tell the user, and do not restart with a forced stream. Until the cause is fixed, the events are in the task output and in `events.jsonl`.
- `dir:`, `state:` and `changelog:` name the resource dir, its `state.json` and its `events.jsonl`. Take paths from here and from the events, never build them.
- `events:` and `ignored:` show the filter; `thresholds:` and `ends:` show the options above.
- `restart with:` is the command to start again with another filter.

## Filters

`--events=<kinds or groups>` reports only those, `--exclude-events=<kinds or groups>` leaves them out. Groups: `status`, `comments`, `push`, `pipeline`; `lifecycle` (`watch_started`, `watch_degraded`, `watch_ended`) is always on. A typo or a kind the resource never produces is a start-up error, not silence.

- An author waiting for CI and review: everything, the default.
- CI only: `--events=pipeline,push`. Leaving out `comments` also saves the thread fetches.
- A review queue: `glab-watch mrs group/project --events=status`.

## The messages

Each batch arrives as one message:

```text
<glab-cli-extras-watcher resource="mr gitlab.example.com/group/project!123" seq="18-19">
{"kind":"head_pipeline_changed","seq":18,"data":{"pipeline_id":456,"status":"running","watch_command":"glab-watch pipeline https://gitlab.example.com/group/project/-/pipelines/456"}}
{"kind":"thread_created","seq":19,"data":{"discussion_id":"6a1f…","author":"reviewer-one","resolvable":true,"resolved":false,"notes":1},"artifacts":["/…/merge_requests/123/threads/2026-11-09T09-30-reviewer-one-6a1f….txt"]}
</glab-cli-extras-watcher>
```

- One JSON event per line. `artifacts` paths are always absolute. Only the first message, the one with `watch_started`, carries `dir="…"` and `changelog="…"`.
- A batch above 64 KB arrives as counts per kind instead of the events, and a gap in `seq` means messages were lost. In both cases read `events.jsonl` from the last `seq` you saw. Delivery is best effort; the changelog has every event.

## Reacting

`references/events.md` lists every kind with its data. The usual reactions:

- **`pipeline_finished` failed** (MR) or **`finished` failed** (pipeline): read `summary.txt` from `artifacts`, or run the event's `watch_command` or `glab-pipeline inspect`. Then follow the glab-pipeline skill.
- **`job_finished` failed**: open the log in `artifacts`.
- **`thread_created`, `thread_updated`**: read only the thread file in `artifacts`, and classify its author with the mr-status skill: human, AI reviewer, the author, your own signed comment.
- **A verdict by an AI reviewer**: read it per the mr-status skill and note the head it names.
- **`pushed`**: compare `to` with the SHA you pushed before you treat it as someone else's push. Verdicts and green pipelines on the old head are stale from now on.
- **`thread_unresolved` with `after_push: true`**: GitLab can show a thread unresolved for a moment after a force-push. Read the thread again before acting on it.
- **`approvals_changed`**: name who approved and whether it is a human or an AI approver.
- **`draft_changed`, `reviewers_changed`**: if the caller defined a handover rule, evaluate it now.
- **`quiet`, `pipeline_stuck`**: act as the caller's purpose says; they report that nothing happened.
- **`watch_degraded`**: `auth`, tell the user to run `glab auth status`; `rate_limited`, nothing to do; `not_found`, check the ref; `daemon_mismatch`, run `glab-watch daemon stop`, and the watchers continue on a new daemon.
- **`watch_ended`**: the reason says why: `merged`, `closed`, `finished`, `until`, `timeout`, `idle`, `not_found`, `session_gone`, or a signal.

### When to ask the user

Ask before acting on a product or design decision, an auth or install problem, a reviewer whose verdict words you cannot read, or anything the user has not already decided in this session. Report state; never promise work.

## Stopping

Stop a watcher by stopping its background task (the task stop tool, or SIGTERM). It also ends on its resource's end, `--until`, `--timeout`, `--idle-after`, and when the session ends. To change a filter, stop the watcher and start the `restart with:` command. Never leave two watchers on the same resource with the same filter.

## The files

Each resource has one dir under the session scratchpad, mirroring its GitLab URL, with a `README.txt` that names its files:

- `state.json`: the latest state; `events.jsonl`: every event; `baseline.json`: the watcher's last snapshot.
- In a pipeline's dir, `job-logs/`: one file per job, the log or a placeholder that says how to download it; and the `glab-pipeline inspect` dump once the pipeline finished.
- In an MR's dir, `threads/`: one file per thread, in glab-discussion's format.

`glab-watch daemon status` shows the shared daemon's subscriptions and API budget; `glab-watch daemon logs` its log.

## Limits

- No ticket states: glab-watch reads GitLab only.
- A session in bypass-permissions mode holds the messages until the user lets them in. The changelog still has every event.
- `mr_updated` of an `mrs` watch is best effort: GitLab does not move `updated_at` for a second change within a minute.
