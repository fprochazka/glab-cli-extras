# glab-cli-extras

Extra commands for the [GitLab CLI](https://gitlab.com/gitlab-org/cli) (`glab`), built for coding agents that wait on CI and review, and just as usable in a terminal or a script:

- `glab-watch` watches a merge request, a pipeline, all merge requests of a project, or a branch, and reports every change as one JSON event per line.
- `glab-pipeline inspect` dumps a pipeline's state and job logs and prints a problem-driven summary.
- `glab-discussion` reads a merge request's discussion threads into one file per thread, and writes comments, replies, resolves and reviews.
- `glab-mr-status` reads merge requests once and reports what changed since the last read, with a one-line scorecard each.
- `glab-cli-extras-cc-hook` is a Claude Code hook that stops an agent from inspecting pipelines or reading and writing MR discussions with raw `glab`, and names the command to use instead.

This package supersedes the standalone [glab-pipeline](https://github.com/fprochazka/glab-pipeline) and [glab-discussion](https://github.com/fprochazka/glab-discussion) packages: their commands live here, with the same options and output.

Those two packages are deprecated. When one of them is still installed next to glab-cli-extras, its command prints one line on stderr before it runs, with the commands that replace it: `uv tool uninstall glab-pipeline && uv tool install glab-cli-extras`. Stdout and the exit code stay the same.

Every GitLab request goes through `glab api`, so `glab` handles authentication, hosts, OAuth refresh, proxies and certificates. No command reads or stores a token.

## Install

You need Python 3.12 or later, [uv](https://docs.astral.sh/uv/), and the `glab` CLI installed and logged in to your GitLab host (`glab auth status`).

```bash
uv tool install glab-cli-extras
```

From git, for the latest unreleased changes:

```bash
uv tool install git+https://github.com/fprochazka/glab-cli-extras
```

From a local checkout:

```bash
uv tool install --editable /path/to/glab-cli-extras
```

## Claude Code plugin

The repository is also a Claude Code plugin marketplace with one plugin, `glab`, which teaches an agent these commands and glab itself:

```bash
uv tool install glab-cli-extras
claude plugin marketplace add fprochazka/glab-cli-extras --scope user
claude plugin install glab@fprochazka-glab-cli-extras --scope user
```

The plugin carries the version of the package it ships with. It replaces the older `glab`, `glab-pipeline` and `glab-discussion` plugins; uninstall those first. See [its README](coding-agent-plugins/claude-code/README.md).

## Where files go

All the commands write into the same tree, one dir per resource, so a file one command wrote is current for the others. The base dir is:

- `<session scratchpad>/glab-cli-extras/` when the command runs under a Claude Code session, which Claude Code removes after a few months;
- else `<temp dir>/glab-cli-extras-<uid>/`, private to the user, which the OS cleans;
- or `--base-dir` for `glab-watch`, and `--output-dir` for `glab-pipeline inspect`, which names the pipeline's dir itself.

The resource dirs mirror GitLab's URLs: `<base>/<host>/<group>/<project>/-/pipelines/<id>/`, `.../-/merge_requests/<iid>/`, `.../-/merge_requests/` for `glab-watch mrs` and `.../-/branches/<branch>/` for `glab-watch branch`. Each one has a `README.txt` that names its files:

- `state.json`: the latest state of the pipeline or MR, written on every change.
- `events.jsonl`: the changelog, every event a watcher reported, and `baseline.json`, the last state this watcher saw.
- `job-logs/<stage>-<name>-<job id>.log` in a pipeline's dir: one file for every job, either the log or a placeholder. See [Job logs](#job-logs).
- `threads/<created>-<author>-<id>.txt` in an MR's dir: one file per discussion thread. A header gives the full discussion id, the type, the file and line of a diff note, `Resolved:` on resolvable threads, and the URL. Then comes one `[<created>] @<username> (note:<id>):` block per note. Bot authors get a `bot-` file name prefix and a `[BOT]` tag, service accounts their display name. A file is rewritten only when the thread changed: a new reply, an edit, a resolve. `.meta.json` maps each discussion id to its file.
- `status-baseline.json`, `status-events.log`, `mr.json` and `external-statuses.json` in an MR's dir: what `glab-mr-status` read. See [glab-mr-status](#glab-mr-status).
- The pipeline dump in a pipeline's dir: `pipeline.json`, `jobs.json`, `bridges.json`, the lint, test report and downstream files when they apply, and `summary.json` and `summary.txt`. See [glab-pipeline](#glab-pipeline).

## glab-watch

`glab-watch` reports every change to a GitLab resource as a JSON event. A shared per-user daemon does the polling, so ten watchers cost about as much API budget as one.

### Usage

```bash
glab-watch mr group/project!123                      # a merge request; also !123 or 123 with the repo from the cwd, or the MR URL
glab-watch pipeline group/project!4567               # a pipeline and its downstream pipelines; also !4567, 4567, or the pipeline URL
glab-watch mrs group/project                         # every merge request of a project
glab-watch branch group/project main                 # a branch; the repo is optional: glab-watch branch main
```

`<repo>` means the same as it does to glab: `group/project`, `group/sub/project`, `host/group/project`, a project URL, or nothing for the repository of the current directory. `glab-watch` asks `glab repo view` to resolve it, which costs one request at start. A URL costs nothing.

A watcher runs until its resource ends (an MR is merged or closed, a pipeline finishes), until `--until`, `--timeout` or `--idle-after` fires, or until it gets SIGTERM or Ctrl-C. `mrs` and `branch` run until stopped. The last event is always `watch_ended` with a `reason`.

The exit code is 0 when the watch ends normally, 1 when the resource is not found or the daemon fails, and 2 for a usage error.

### Options

All four resource commands take:

- `--events=<kinds or groups>`: report only these. Comma-separated, repeatable. The default is every event the resource tracks.
- `--exclude-events=<kinds or groups>`: do not report these.
- `--until=<kind>`: stop after the first event of this kind, for example `--until=finished`.
- `--timeout=<duration>`: stop after `90s`, `30m`, `2h` or `1h30m`, counted from the start.
- `--idle-after=<duration>`: stop when nothing was reported for this long. Every reported event starts the count again, except `quiet` and `pipeline_stuck`, which report that nothing happened. The watch ends with `watch_ended` reason `idle`.
- `--events-stream=auto|stdout|claude-message`: where events go. See [Output streams](#output-streams).
- `--base-dir=<path>`: where the resource dirs go. See [Where files go](#where-files-go).
- `--message-priority=now|next|later`: for `claude-message` only. The default is `next`.
- `--no-daemon`: poll inside this process, with a private budget, without the shared daemon.

`pipeline` also takes `--logs=failed|all|none`: which finished jobs get their log downloaded. The default is `failed`. `mr` does not take `--logs`; follow the `watch_command` of a pipeline event for the jobs.

Two thresholds report what did not happen. The watcher computes them from what it already receives, so they cost no request:

- `--quiet-after=<duration>` on `mr`, repeatable: a `quiet` event when the head pipeline succeeded on the current head and nothing else was reported for that long. It comes once per head SHA and threshold. A push, a new green pipeline and every other reported event start the count again. `--quiet-after=2m --quiet-after=15m` says "green and quiet for 2 minutes", then again for 15.
- `--stuck-after=<duration>` on `mr` and `pipeline`: a `pipeline_stuck` event when the pipeline is still `running` or `pending` that long after the watcher first saw it in one of those statuses. It comes once per pipeline id.

Without its option, neither event is reported, and asking for it with `--events` is an error. With its option, the event must pass the filter: `--quiet-after` with `--exclude-events=status` is an error too.

### Startup header

On start, every watcher prints a header. Each line starts with `glab-watch: `:

```text
glab-watch: resource: mr gitlab.example.com/group/project!123
glab-watch: stream: stdout (one JSON event per line)
glab-watch: dir: /tmp/glab-cli-extras-1000/gitlab.example.com/group/project/-/merge_requests/123
glab-watch: state: /tmp/glab-cli-extras-1000/gitlab.example.com/group/project/-/merge_requests/123/state.json
glab-watch: changelog: /tmp/glab-cli-extras-1000/gitlab.example.com/group/project/-/merge_requests/123/events.jsonl
glab-watch: events: state_changed,draft_changed,pushed,… (always: watch_started,watch_degraded,watch_ended)
glab-watch: ignored: thread_created,thread_updated,thread_resolved,thread_unresolved
glab-watch: daemon: pid 4242, endpoint /run/user/1000/glab-cli-extras/daemon-v3.sock, log /run/user/1000/glab-cli-extras/daemon-v3.log
glab-watch: restart with: glab-watch mr https://gitlab.example.com/group/project/-/merge_requests/123 --events=<kinds|groups> --exclude-events=<kinds|groups> (groups: status, comments, push, pipeline)
```

The `state:` line appears for `mr` and `pipeline` only. A watch with `--quiet-after` or `--stuck-after` adds a `thresholds:` line, a `pipeline` watch adds a `logs:` line, a watch with `--until`, `--timeout` or `--idle-after` adds an `ends:` line, and a `stream fallback:` line says why `auto` did not message the session.

The header and the other status lines (`ended: …`, errors) always go to stderr, so stdout carries only events. Claude Code puts a background task's stderr into the task output next to its stdout, so an agent sees both. A status line never starts with `{`: in merged output, every line that starts with `{` is an event.

### Events

Every event has `kind`, `resource`, `seq`, `observed_at` and `data`, and `artifacts` when it points at files:

```json
{"kind":"job_finished","resource":"pipeline gitlab.example.com/group/project!4567","seq":7,"observed_at":"2026-11-09T10:05:03.120Z","data":{"pipeline_id":4567,"job_id":901,"name":"unit-tests","stage":"test","status":"failed","log_truncated":false},"artifacts":["/tmp/glab-cli-extras-1000/gitlab.example.com/group/project/-/pipelines/4567/job-logs/test-unit-tests-901.log"]}
```

`seq` numbers the events a watcher emits: it starts after the last `seq` in the changelog and only goes up, also when the daemon restarts in between.

Lifecycle events come with every resource and cannot be filtered out:

- `watch_started`: the current state, in `data.summary`.
- `watch_degraded`: polling has trouble: `rate_limited`, `auth`, `not_found` or `error`, with a message. It is sent once per kind of trouble, not on every failed poll. The reason `daemon_mismatch` means the running daemon does not know a request this `glab-watch` sends, because it was started from other code of the same protocol version (an editable install that changed since). The watch keeps reporting events, but the files that need the daemon (job logs, thread files, the pipeline dump) stay out of date until you run `glab-watch daemon stop`; the running watchers then start a current daemon and subscribe again.
- `watch_ended`: with a `reason`: `merged`, `closed`, `finished`, `not_found`, `until`, `timeout`, `idle`, `session_gone`, or the signal.

`mr`:

- `state_changed` (`from`, `to`, and `reopened: true` for closed to opened), `draft_changed`, `title_changed`, `description_changed`
- `pushed`: `from`, `to`, `forced`. A force-push is detected with one `merge_base` request per push.
- `merge_status_changed`: GitLab's transient `checking`, `unchecked` and `preparing` are skipped, and so is every change once the MR is merged or closed.
- `approvals_changed`, `reviewers_changed`
- `head_pipeline_changed`, `pipeline_status_changed` (`from`, `to`) and `pipeline_finished` (with `failed_jobs` when it failed) for the head pipeline. These are pipeline-level only: an MR watch reports no job events and downloads no logs. Each one has the pipeline's `web_url` and `watch_command`, the `glab-watch pipeline <url>` command that watches it job by job. The head pipeline's status comes with the MR's own snapshot query, so it costs no extra request.
- `thread_created`, `thread_updated`, `thread_resolved`, `thread_unresolved`, each with the thread's file in `artifacts`. GitLab can show a thread unresolved for a poll or two after a force-push; a `thread_unresolved` within two polls of a `pushed` has `after_push: true`.
- `quiet` (`after`, `head_sha`, `pipeline_id`) with `--quiet-after`, and `pipeline_stuck` (`after`, `pipeline_id`, `status`, `web_url`, `watch_command`) with `--stuck-after`.

One `glab-discussion drafts publish` changes the MR in up to three ways at once: a review state, `reviewers_changed` when it added you as a reviewer, and `approvals_changed` when it revoked your approval. A watcher of your own MR sees these as events like any other.

`pipeline`: `status_changed`, `job_started`, `job_finished` (with the log in `artifacts` when `--logs` asks for it), `downstream_created`, `finished` (with `summary.txt` in `artifacts`, and the pipeline's dir in `data.dir`), and `pipeline_stuck` (`after`, `pipeline_id`, `status`, `web_url`, `running_jobs`) with `--stuck-after`. When the pipeline finishes, the watcher writes the dump `glab-pipeline inspect` writes, and the final `watch_ended` also carries `summary.txt` in `artifacts`. A later `glab-pipeline inspect` of the same pipeline in the same session finds the files current and makes one request, the ETag probe.

`mrs`: `mr_opened`, `mr_updated`, `mr_merged`, `mr_closed`, `mr_reopened`. When the list shows what moved, the event has `changes: {field: [old, new]}` for `title`, `draft`, `target_branch`, `sha`, `detailed_merge_status`, `labels`, `assignees` and `reviewers`, for example `{"draft": [true, false]}` when a draft becomes ready. These come from the same list request, so they cost nothing extra.

`branch`: `pushed`, `force_pushed`, `created`, `deleted`, `pipeline_created`, `pipeline_finished`. A push whose head commit is a merge commit has `merge_commit: true` and, when GitLab knows it, `merged_mr_iid`.

`--events` and `--exclude-events` take kinds and these groups:

- `status`: `state_changed`, `draft_changed`, `merge_status_changed`, `approvals_changed`, `reviewers_changed`, `title_changed`, `description_changed`, `quiet`, and all `mr_*`.
- `comments`: `thread_*`.
- `push`: `pushed`, `force_pushed`, `created`, `deleted`.
- `pipeline`: `head_pipeline_changed`, `pipeline_status_changed`, `status_changed`, `job_*`, `downstream_created`, `finished`, `pipeline_created`, `pipeline_finished`, `pipeline_stuck`.

A group brings `quiet` and `pipeline_stuck` only when their option is given.
- `lifecycle`: `watch_started`, `watch_degraded`, `watch_ended`; always on.

The filter is checked at start. An unknown kind or group, or one the resource never produces, is an error, so a typo does not silently mean "no events". The filter also saves API work: an MR watch without `comments` never fetches threads, and one without `pipeline` leaves the head pipeline out of its query.

### Output streams

`--events-stream=stdout` prints one JSON event per line on stdout. Whatever the stream, the changelog `events.jsonl` in the resource dir keeps every reported event, also when nobody read the stream.

`--events-stream=claude-message` sends the events to a Claude Code session as messages, through the session's messaging socket (`CLAUDE_CODE_MESSAGING_SOCKET`). It fails at start when no session accepts messages. The default, `--events-stream=auto`, picks `claude-message` when the watcher runs under a Claude Code session that accepts messages, and stdout otherwise. To use it, start the watcher as a background Bash task from the session:

```bash
glab-watch mr group/project!123 --exclude-events=comments
```

The session sees each batch as a message from `glab-watch mr group/project!123`:

```text
<glab-cli-extras-watcher resource="mr gitlab.example.com/group/project!123" seq="18-19">
{"kind":"head_pipeline_changed","seq":18,"data":{"pipeline_id":456,"web_url":"https://gitlab.example.com/group/project/-/pipelines/456","watch_command":"glab-watch pipeline https://gitlab.example.com/group/project/-/pipelines/456","status":"running","previous_pipeline_id":455}}
{"kind":"thread_created","seq":19,"data":{"discussion_id":"6a1f…","author":"reviewer-one","resolvable":true,"resolved":false,"notes":1},"artifacts":["/tmp/claude-1000/…/scratchpad/glab-cli-extras/…/merge_requests/123/threads/2026-11-09T09-30-reviewer-one-6a1f….txt"]}
</glab-cli-extras-watcher>
```

- **Paths.** The first message, the one with `watch_started`, also carries `dir="…"` and `changelog="…"`: the resource dir and its `events.jsonl`. Its `watch_started` gives the threads dir relative to that dir, and stdout and `events.jsonl` keep the full paths. Later messages leave the paths out, also after a daemon restart; each event's `artifacts` keep their absolute paths, so a file opens without the first message.
- **Batching.** Events that arrive close together go out as one message: the first event of a burst waits up to 3 s for the rest. A batch above 64 KB is sent as a count per kind; the changelog has the full events.
- **Priority.** `--message-priority=next` (the default) delivers into the session's running turn, at its next tool round. `now` also delivers into the running turn, ahead of queued work. `later` waits until the session is idle and then starts a new turn.
- **Rate limits.** Each watcher sends as `glab-watch:<pid>`, and keeps below the session's per-sender limit; messages it cannot send yet are merged into the next one.
- **Files.** Under a Claude Code session every file goes to `<session scratchpad>/glab-cli-extras/`, in both stream modes.
- **Lifetime.** A `claude-message` watcher exits when its session is gone: the socket refuses the message, or the session's record is no longer live. Watchers never outlive their session.
- **Subagents.** A watcher started from a subagent's Bash messages the main session. Claude Code has no route to a subagent.
- **Best effort.** The session may drop a message under its own rate limit, or hold it until its user approves it, which a session in bypass-permissions mode does. The changelog always has every event.

When the session's messaging does not match what the cc-blackbox SDK speaks, for example after a Claude Code update, `auto` uses stdout, and the header says why on a `glab-watch: stream fallback:` line. If that happens while the watcher runs, it prints the same line and sends the rest of the events to stdout.

### The daemon

The first watcher starts the daemon, `python -m glab_cli_extras.daemon`, as a detached process. It exits 30 s after its last watcher is gone. You never have to start it. If it cannot start, the watcher exits and suggests `--no-daemon`.

```bash
glab-watch daemon status      # subscriptions, resources, request counts and budget per host; --json for everything
glab-watch daemon logs -n 100 # the end of the daemon's log, 50 lines by default
glab-watch daemon stop        # running watchers start a new daemon on their next poll and carry on
```

- **One daemon per user.** It holds an OS lock on `daemon-v3.lock` in its state dir (`$XDG_RUNTIME_DIR/glab-cli-extras/` on Linux, `$TMPDIR/glab-cli-extras/` on macOS, `%LOCALAPPDATA%\glab-cli-extras\run\` on Windows). Watchers that start at the same moment start one daemon, not one each.
- **Leases.** Each watcher polls the daemon every 5 s, which renews its subscriptions. A watcher that dies stops costing anything 20 s later.
- **Restarts.** If the daemon goes away, watchers start a new one and subscribe again. The new daemon compares GitLab's state with the snapshot each watcher kept, so a change made in between is still reported, and nothing is reported twice.
- **Budget.** The daemon paces each host on GitLab's `RateLimit-Remaining` and `RateLimit-Reset` headers and uses at most half of what is left. A host without those headers gets 60 requests a minute. When the budget is short, every poll interval grows by the same factor. A 429 pauses the whole host for its `Retry-After`.
- **Cheap probes.** Pipelines are polled through GitLab's GraphQL ETag cache: an unchanged pipeline answers with a 304 before any query runs. Base intervals: pipeline 5 s while running, MR 30 s (15 s on hosts with a high rate limit), branch pipelines 10 s, branch pushes 30 s, `mrs` 60 s.
- **Sharing.** Watchers of the same resource share one poller. All watched branches of a project share one push-event request.
- **No files.** The daemon writes no files. It fetches each log and each changed thread once, keeps it in memory for a while, and hands it to every watcher that asks, so ten watchers of one pipeline cost one download. The watchers write the files.

## glab-pipeline

```bash
glab-pipeline inspect                              # the head pipeline of the current branch's MR
glab-pipeline inspect --pipeline-url <url>         # an explicit pipeline
glab-pipeline inspect --pipeline-id 1234567 --project group/project --hostname gitlab.example.com
glab-pipeline inspect --mr-url <url>               # an MR's head pipeline; also --mr-iid with --project and --hostname
glab-pipeline inspect --logs=all                   # every job's log, not only the failed jobs' logs
glab-pipeline inspect --with-merged-ci-config      # two-step lint that resolves include: against the source branch
glab-pipeline inspect --with-test-report           # the test report even without a failed test job
glab-pipeline inspect --with-downstream-pipelines  # downstream detail for every bridge, not only failed ones
glab-pipeline inspect --with-artefacts             # download and unpack each job's artifacts archive
glab-pipeline inspect --json | jq                  # the summary as JSON instead of text
glab-pipeline inspect --output-dir /path/to/dir    # write the dump there instead of the pipeline's dir
glab-pipeline inspect --full                       # ignore what is on disk and fetch everything again
```

`inspect` writes the dump into the pipeline's dir (see [Where files go](#where-files-go)), the same dir `glab-watch pipeline` writes, and prints a problem-driven summary:

- `pipeline.json`, `jobs.json` (retried jobs included) and `bridges.json` (when there are bridges): the GitLab API objects.
- `job-logs/<stage>-<name>-<id>.log`: the log of every failed job, and a placeholder for every other job. `--logs=all` downloads every log, `--logs=none` none.
- `lint.json` and `merged.yml` when the pipeline has YAML errors, no jobs, or a job failed for a config reason; `test-report.json` when a test job failed; `downstream/<bridge>-<id>.json` for a failed bridge; `artifacts/<stage>-<name>-<id>/` with `--with-artefacts`.
- `summary.json` and `summary.txt`: the summary, also printed on stdout. `summary.json` is written also without `--json`.

A rerun reuses what is on disk. The pipeline is probed through GitLab's GraphQL ETag cache with the ETag of the last dump; an unchanged pipeline answers with a 304 and the rerun makes no other request. A finished job's log is never downloaded twice.

The context flags are a URL, or `--pipeline-id`/`--mr-iid` with `--project` and `--hostname`, or nothing for the current branch's MR. A partial set, such as `--project` without `--hostname`, is an error instead of being silently ignored. `--pipeline-id` alone takes the project of the current branch's MR.

### Job logs

`inspect` and `glab-watch pipeline` write the same `job-logs/` dir, one file for every job, and take the same `--logs=failed|all|none`, default `failed`. A log is downloaded once; `glab-watch` downloads it a few seconds after the job ends. The log of a downstream job goes to its own pipeline's dir.

A job whose log is not downloaded, because it passed and `--logs` is `failed`, or because it is still running, pending, skipped or manual, has a short placeholder in its place. The placeholder starts with `[glab-cli-extras: log not downloaded]`, says why, and gives the `glab-pipeline inspect … --logs=all` command that downloads it. `state.json` records `log_state` for each job: `downloaded`, `tail` or `placeholder`.

On GitLab 19.0 and later, a log above 500 KB is read as its last 500 KB only, and the file starts with a `[glab-cli-extras: last …]` line.

## glab-discussion

```bash
glab-discussion read                     # the current branch's MR: one file per thread, then a summary of what changed
glab-discussion read --mr-url <url>      # an explicit MR; also --mr-iid with --project and --hostname
glab-discussion read --no-dump           # print every thread instead (the default in a terminal)
glab-discussion read --full              # delete the thread files and write them all again
glab-discussion write --body - < reply.md --reply-to <discussion id>     # reply to a thread
glab-discussion write --body "…" --file src/app.py --new-line 42         # a diff note on a line of the newest diff version
glab-discussion write --body "…" --draft                                 # a pending draft only you see
glab-discussion resolve <discussion id>                                  # also --unresolve
glab-discussion edit note:123 --body "…"                                 # or draft:123, as read prints the ids
glab-discussion delete note:123
glab-discussion diff --file src/app.py                                   # the diff with old and new line numbers; --version for an older diff version
glab-discussion drafts publish --body - < summary.md --verdict reviewed  # publish your drafts as one review
glab-discussion drafts delete --force                                    # without --force, list your drafts
```

`write` also takes `--old-line` and `--commit` for a diff note, and `--resolve` with `--reply-to` and `--draft` to resolve the thread when the draft is published. `--no-draft` publishes at once.

`read --dump` writes the threads into the `threads/` dir of the MR's dir (see [Where files go](#where-files-go)), the same dir `glab-watch mr` writes, and prints the dir and the files that changed:

```text
Discussions: /tmp/glab-cli-extras-1000/gitlab.example.com/group/project/-/merge_requests/123/threads/
  updated: 2026-11-09T09-30-reviewer-one-6a1f2b3c4d5e.txt
  (11 discussions up to date)
```

A thread file is fetched again only when its notes changed since the file was written, or when your draft replies in it changed. An unchanged MR costs one GraphQL request per 100 threads and one request for your drafts. Your drafts appear as `[DRAFT]` blocks in their thread, and a draft that starts a thread as `draft-<id>.txt`. Drafts belong to the login that wrote them, so only `read` shows them: a `glab-watch mr` rewrites a changed thread without your draft replies, and the next `read` puts them back.

The context flags are all or nothing: `--mr-url`, or `--mr-iid` with `--project` and `--hostname`, or none of them for the current branch's MR. A partial set is a usage error instead of silently using the current branch's MR.

### Writes

The write commands send every body as JSON on stdin, byte for byte: a trailing signature line such as `<!-- plugin:command -->` and the final newline reach GitLab unchanged. A write is never retried, because GitLab may have applied a write whose answer was lost, and a retry would post it twice. Each write command prints the URL of the MR it acted on. `GLAB_DISCUSSION_WRITE_AS_DRAFT=true` makes `write` save drafts unless `--no-draft` is given.

Before a write, the commands check what agents most often got wrong, and stop with exit 1 and a message that says what to do instead:

- An empty or blank body, as from `--body "$(cat <missing file>)"`, is refused. Prefer `--body - < file`, which fails in the shell when the file is missing.
- `write --file` anchors the note on the diff: on an unchanged line it adds the other side's line number, which GitLab requires there (`--new-line` alone is enough); on a renamed file it uses the old path. A line outside every hunk is refused, since GitLab takes diff notes only on changed lines and their context. If GitLab still creates a general comment, the command reports it.
- `--reply-to` and `resolve` take the 12-character id prefix from a thread file's name too, resolved against the MR's files without a request. The full id from the `Discussion:` line is never ambiguous.
- `resolve` reads the thread first and refuses one without a resolvable note, which GitLab answers with 403 or 404. A reply or resolve that gets 404 says whether the thread is gone (deleted, or rewritten by a force-push) or the error was transient.

`drafts publish` submits the review with GitLab's `/submit_review` quick action. It publishes your drafts and sets the verdict on every GitLab since 16.7, where the `bulk_publish` endpoint ignores the verdict before 19.2. Before it submits, it adds you as a reviewer if you are not one and checks that GitLab did. It also removes your earlier approval or request for changes that the new verdict replaces, because GitLab before 19.2 does not replace one verdict with another. Without `--verdict` it sets `reviewed`, but only when you have given the MR no verdict yet; otherwise `--verdict` is required. Your earlier verdict comes from the MR's system notes, because a draft resets your review state. Afterwards it reads the drafts, your review state and the approvals back, and exits with 1 when the verdict was not applied.

`edit draft:<id>` sends an inline draft's position back with the new text, because GitLab since 16.3 turns an inline draft into a general one on an edit without it.

## glab-mr-status

```bash
glab-mr-status                                  # the current branch's MR
glab-mr-status group/project!123 !124           # any glab-watch MR ref, or an MR URL; several at once
glab-mr-status --mr-url <url> --mr-url <url>    # the same, as URLs
glab-mr-status --comments                       # only the threads; --pipeline only the pipeline dump; --all both (the default)
glab-mr-status --reset                          # forget the last read and start a new baseline
glab-mr-status --base-dir <path>                # as glab-watch
```

`glab-mr-status` answers "what is this MR like now, and what changed since I last looked". To be told when something changes, run `glab-watch mr` instead of reading in a loop.

The first read of an MR establishes a baseline and prints its state. Every later read prints one `<ID>: <detail>` line per change since the read before, then the threads, the pipeline dump and a scorecard:

```text
== gitlab.example.com/group/project!123
  HEAD_CHANGED: 3f2a6c0d9b1e4f7a8c2d5e6f7a8b9c0d1e2fe91c -> 8b7d04aa5c3e2f1a0b9c8d7e6f5a4b3c2d1e04aa (new head)
  PIPELINE_CHANGED: new pipeline 4568 (running) on 8b7d04aa, was 4567 (failed)
  NOTES_CHANGED: new note by @reviewer-one at 2026-11-09T10:12:00Z
  discussions: /tmp/glab-cli-extras-1000/gitlab.example.com/group/project/-/merge_requests/123/threads/ (one file per thread, …)
    new: 2026-11-09T10-12-reviewer-one-6a1f2b3c4d5e.txt
  pipeline dump: /tmp/glab-cli-extras-1000/gitlab.example.com/group/project/-/pipelines/4568/ (summary.json, jobs.json, job-logs/) · summary text: …/summary.txt
  scorecard: gitlab.example.com/group/project!123 · opened · ready · head 8b7d04aa · pipeline running · behind 0 · conflicts no · notes 4 · approved by - · updated 2026-11-09T10:12:00Z
state: /tmp/glab-cli-extras-1000/gitlab.example.com/group/project/-/merge_requests/123
```

The change ids are `STATE_CHANGED`, `DRAFT_CHANGED`, `HEAD_CHANGED` (full SHAs, with a rebase hint when the behind count dropped to 0), `TARGET_CHANGED`, `TITLE_CHANGED`, `PIPELINE_CHANGED` (noting a pipeline that ran on a superseded base), `BEHIND_CHANGED`, `MERGE_STATUS_CHANGED`, `NOTES_CHANGED`, `DISCUSSIONS_RESOLVED_CHANGED`, `REVIEWERS_CHANGED`, `ASSIGNEES_CHANGED`, `LABELS_CHANGED`, `APPROVALS_CHANGED`, and `READ_FAILED` when an MR could not be read. In the scorecard, `approved by ?` means GitLab did not return the approvals, and `(on an older head)` means the pipeline ran on a commit that is no longer the head.

Each read costs the REST merge request, its newest note, its approvals when the MR moved since the last read, and the project's default branch once. The threads cost what `glab-discussion read` costs and the pipeline dump what `glab-pipeline inspect` costs, which is two requests and one request when nothing changed. `--comments` or `--pipeline` leaves the other out.

The read is printed and flushed before it is stored, so a read killed in between reports the same changes again. It stores into the MR's dir:

- `status-baseline.json`: the last state this command read. A running `glab-watch` keeps its own `baseline.json`, so neither swallows a change the other has not reported.
- `status-events.log`: every change it printed, one tab-separated line each: UTC time, MR, id, detail.
- `mr.json`: the REST merge request; `external-statuses.json`: the head commit's statuses its pipeline did not produce, such as an outside quality gate.
- the `threads/` dir and, in the pipeline's dir, the pipeline dump, as `glab-discussion read` and `glab-pipeline inspect` write them.

The exit code is 0 for a baseline or a read, 5 when no MR could be read, 1 for an error such as no MR on the current branch, and 2 for a usage error. When some MRs of a set fail and others are read, the exit code is 0 and each failed MR has a `READ_FAILED` line.

## The Claude Code hook

`glab-cli-extras-cc-hook` is a PreToolUse hook for the Bash tool. It denies a command that inspects pipelines, jobs or job logs with `glab ci view|get|trace` or the raw API (`glab api projects/…/pipelines/…`, `…/jobs/…/trace`, `…/ci/lint`), or that reads or writes MR discussions with `glab api …/notes|discussions|draft_notes`, `glab mr view --comments` or `glab mr note`. The deny reason names the rule that matched, the command to use instead and the skill that teaches it. Actions stay allowed: `glab ci list`, `glab ci status`, `glab ci run`, `glab ci retry`, the pipelines list endpoint, award emoji. The rules ship in the package, in `glab_cli_extras/cc_hook/rules/`.

To register it, add it to a Claude Code settings file:

```json
{"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "glab-cli-extras-cc-hook", "timeout": 10}]}]}}
```

[bash-classify](https://github.com/fprochazka/bash-classify) parses the command, so a blocked command inside a wrapper (`sudo`, `timeout`, `bash -c`, `xargs`, `eval`) or behind glab's `pipe`/`pipeline` aliases is caught, and text that only names one (a heredoc, an `echo`, a commit message) is not. When bash-classify cannot fully parse a command, the hook falls back to text patterns, which also fire on such text, and the deny reason says so.

Claude Code starts the hook on every Bash call. A command that does not contain `glab` is answered without parsing, in about 10 ms over the interpreter's own start; one that does takes about 55 ms over it, most of it bash-classify's import and its YAML command definitions. A hook that crashes prints nothing and exits 1, which Claude Code shows as a hook error and treats as no opinion.

## Platform support

- Linux and macOS: everything. The daemon listens on a Unix socket.
- Windows: the daemon and the stdout stream. The daemon listens on a loopback TCP port, and every request carries a random token from a file only the user can read. The `claude-message` stream is not available.

## Limitations

- **No `claude-message` on Windows.** The SDK has no Windows transport yet; `auto` picks stdout there.
- **One glab process per request.** Starting `glab` costs tens of milliseconds. At these request rates, a few per second at most, that does not matter.
- **The free ETag 304 is not documented by GitLab.** If GitLab starts to count those requests, pipeline watching gets slower under the budget, not more expensive.
- **Branch pipeline probes are cheap only on quiet projects.** GitLab touches the ETag key of a project's pipelines on any pipeline change in the project. In a busy monorepo nearly every 10 s probe is a full request.
- **`mr_updated` is best effort.** GitLab does not move an MR's `updated_at` when it was touched less than a minute before, so a second change within that minute shows up only with the next change.
- **Job logs of large jobs need GitLab 19.0** for the tail-only download. Older versions download the whole log.
- **Deeper downstream pipelines** (a child of a child) are not watched. Their changes do reach the watched pipeline's ETag, but their jobs are not reported.
- **SSH host aliases** in git remotes are resolved by glab, as glab resolves them. A remote glab cannot map to a configured host fails at start.

## Development

```bash
uv sync
uv run ruff check
uv run ruff format --check
uv run mypy
uv run pytest
```

To run against a local checkout of [cc-blackbox](https://github.com/fprochazka/cc-blackbox) instead of its release, for example while changing both, layer it over the locked version for one command. This leaves `pyproject.toml` and `uv.lock` alone:

```bash
uv run --with-editable ../cc-blackbox pytest
```

The tests need no network and no glab login. A fake `glab` (`tests/fakeglab.py`) answers with the response shapes recorded from a GitLab 19.5 instance, in process for the pollers and as a real executable on `PATH` for the daemon tests, which start real daemons in temporary dirs.

## Changelog

See [CHANGELOG.md](https://github.com/fprochazka/glab-cli-extras/blob/master/CHANGELOG.md).

## License

MIT
