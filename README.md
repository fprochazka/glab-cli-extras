# glab-cli-extras

Extra commands for the [GitLab CLI](https://gitlab.com/gitlab-org/cli) (`glab`), built for coding agents that wait on CI and review, and just as usable in a terminal or a script:

- `glab-watch` watches a merge request, a pipeline, all merge requests of a project, or a branch, and reports every change as one JSON event per line.
- `glab-pipeline inspect` dumps a pipeline's state and job logs and prints a problem-driven summary.

Every GitLab request goes through `glab api`, so `glab` handles authentication, hosts, OAuth refresh, proxies and certificates. `glab-watch` never reads or stores a token. A shared per-user daemon does the polling, so ten watchers cost about as much API budget as one.

## Install

You need Python 3.11 or later, [uv](https://docs.astral.sh/uv/), and `glab` logged in to your GitLab host (`glab auth status`).

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

## Usage

```bash
glab-watch mr group/project!123                      # a merge request; also !123 or 123 with the repo from the cwd, or the MR URL
glab-watch pipeline group/project!4567               # a pipeline and its downstream pipelines; also the pipeline URL
glab-watch mrs group/project                         # every merge request of a project
glab-watch branch group/project main                 # a branch; the repo is optional: glab-watch branch main
```

`<repo>` means the same as it does to glab: `group/project`, `group/sub/project`, `host/group/project`, a project URL, or nothing for the repository of the current directory. `glab-watch` asks `glab repo view` to resolve it, which costs one request at start. A URL costs nothing.

A watcher runs until its resource ends (an MR is merged or closed, a pipeline finishes), until `--until` or `--timeout` fires, or until it gets SIGTERM or Ctrl-C. `mrs` and `branch` run until stopped. The last event is always `watch_ended` with a `reason`.

### Options

All commands:

- `--events=<kinds or groups>`: report only these. Comma-separated, repeatable. The default is every event the resource tracks.
- `--exclude-events=<kinds or groups>`: do not report these.
- `--until=<kind>`: stop after the first event of this kind, for example `--until=finished`.
- `--timeout=<duration>`: stop after `90s`, `30m`, `2h` or `1h30m`.
- `--events-stream=auto|stdout|claude-message`: where events go. See [Claude Code](#claude-code).
- `--base-dir=<path>`: where the resource dirs go. See [Files](#files).
- `--message-priority=now|next|later`: for `claude-message` only. The default is `next`.
- `--no-daemon`: poll inside this process, with a private budget, without the shared daemon.

`pipeline` also takes `--logs=failed|all|none`: which finished jobs get their log downloaded. The default is `failed`.

The filter is checked at start. An unknown kind or group, or one the resource never produces, is an error, so a typo does not silently mean "no events". The filter also saves API work: an MR watch without `comments` never fetches threads, and one without `pipeline` never polls the head pipeline or downloads job logs.

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

It is the command of the [glab-pipeline](https://github.com/fprochazka/glab-pipeline) package, with the same options and output. It writes the dump into the pipeline's dir (see [Files](#files)), the same dir `glab-watch` writes, and prints a problem-driven summary:

- `pipeline.json`, `jobs.json` (retried jobs included) and `bridges.json` (when there are bridges): the GitLab API objects.
- `job-logs/<stage>-<name>-<id>.log`: the log of every failed job, and a placeholder for every other job. `--logs=all` downloads every log.
- `lint.json` and `merged.yml` when the pipeline has YAML errors, no jobs, or a job failed for a config reason; `test-report.json` when a test job failed; `downstream/<bridge>-<id>.json` for a failed bridge; `artifacts/<stage>-<name>-<id>/` with `--with-artefacts`.
- `summary.json` and `summary.txt`: the summary, also printed on stdout.

A rerun reuses what is on disk. The pipeline is probed through GitLab's GraphQL ETag cache with the ETag of the last dump; an unchanged pipeline answers with a 304 and the rerun makes no other request. A finished job's log is never downloaded twice.

The context flags follow glab-pipeline: a URL, or `--pipeline-id`/`--mr-iid` with `--project` and `--hostname`, or nothing for the current branch's MR. A partial set, such as `--project` without `--hostname`, is an error instead of being silently ignored. `--pipeline-id` alone takes the project of the current branch's MR.

## Events

Every event has `kind`, `resource`, `seq`, `observed_at` and `data`, and `artifacts` when it points at files:

```json
{"kind":"job_finished","resource":"pipeline gitlab.example.com/group/project!4567","seq":7,"observed_at":"2026-11-09T10:05:03.120Z","data":{"pipeline_id":4567,"job_id":901,"name":"unit-tests","stage":"test","status":"failed","log_truncated":false},"artifacts":["/tmp/glab-cli-extras-1000/gitlab.example.com/group/project/-/pipelines/4567/job-logs/test-unit-tests-901.log"]}
```

`seq` numbers the events a watcher emits: it starts after the last `seq` in the changelog and only goes up, also when the daemon restarts in between.

Lifecycle events come with every resource and cannot be filtered out:

- `watch_started`: the current state, in `data.summary`.
- `watch_degraded`: polling has trouble: `rate_limited`, `auth`, `not_found` or `error`, with a message. It is sent once per kind of trouble, not on every failed poll. The reason `daemon_mismatch` means the running daemon does not know a request this `glab-watch` sends, because it was started from other code of the same protocol version (an editable install that changed since). The watch keeps reporting events, but the files that need the daemon (job logs, thread files, the pipeline dump) stay out of date until you run `glab-watch daemon stop`; the running watchers then start a current daemon and subscribe again.
- `watch_ended`: with a `reason`: `merged`, `closed`, `finished`, `not_found`, `until`, `timeout`, `session_gone`, or the signal.

`mr`:

- `state_changed` (`from`, `to`, and `reopened: true` for closed to opened), `draft_changed`, `title_changed`, `description_changed`
- `pushed`: `from`, `to`, `forced`. A force-push is detected with one `merge_base` request per push.
- `merge_status_changed`: GitLab's transient `checking`, `unchecked` and `preparing` are skipped, and so is every change once the MR is merged or closed.
- `approvals_changed`, `reviewers_changed`
- `head_pipeline_changed`, `pipeline_status_changed` (`from`, `to`) and `pipeline_finished` (with `failed_jobs` when it failed) for the head pipeline. These are pipeline-level only: an MR watch reports no job events and downloads no logs. Each one has the pipeline's `web_url` and `watch_command`, the `glab-watch pipeline <url>` command that watches it job by job. The head pipeline's status comes with the MR's own snapshot query, so it costs no extra request.
- `thread_created`, `thread_updated`, `thread_resolved`, `thread_unresolved`, each with the thread's file in `artifacts`. GitLab can show a thread unresolved for a poll or two after a force-push; a `thread_unresolved` within two polls of a `pushed` has `after_push: true`.

One `glab-discussion drafts publish` changes the MR in up to three ways at once: a review state, `reviewers_changed` when it added you as a reviewer, and `approvals_changed` when it revoked your approval. A watcher of your own MR sees these as events like any other.

`pipeline`: `status_changed`, `job_started`, `job_finished` (with the log in `artifacts` when `--logs` asks for it), `downstream_created`, `finished` (with `summary.txt` in `artifacts`).

`mrs`: `mr_opened`, `mr_updated`, `mr_merged`, `mr_closed`, `mr_reopened`. When the list shows what moved, the event has `changes: {field: [old, new]}` for `title`, `draft`, `target_branch`, `sha`, `detailed_merge_status`, `labels`, `assignees` and `reviewers`, for example `{"draft": [true, false]}` when a draft becomes ready. These come from the same list request, so they cost nothing extra.

`branch`: `pushed`, `force_pushed`, `created`, `deleted`, `pipeline_created`, `pipeline_finished`. A push whose head commit is a merge commit has `merge_commit: true` and, when GitLab knows it, `merged_mr_iid`.

Groups for `--events` and `--exclude-events`:

| Group | Kinds |
|---|---|
| `status` | `state_changed`, `draft_changed`, `merge_status_changed`, `approvals_changed`, `reviewers_changed`, `title_changed`, `description_changed`, and all `mr_*` |
| `comments` | `thread_*` |
| `push` | `pushed`, `force_pushed`, `created`, `deleted` |
| `pipeline` | `head_pipeline_changed`, `pipeline_status_changed`, `status_changed`, `job_*`, `downstream_created`, `finished`, `pipeline_created`, `pipeline_finished` |
| `lifecycle` | `watch_started`, `watch_degraded`, `watch_ended`; always on |

### Files

Every watcher writes what it reports into one dir per resource, under a base dir:

- `<session scratchpad>/glab-cli-extras/` when it runs under a Claude Code session, which Claude Code removes after a few months;
- else `<temp dir>/glab-cli-extras-<uid>/`, private to the user, which the OS cleans;
- or `--base-dir`.

The resource dirs mirror GitLab's URLs: `<base>/<host>/<group>/<project>/-/pipelines/<id>/`, `.../-/merge_requests/<iid>/`, `.../-/merge_requests/` for `mrs` and `.../-/branches/<branch>/` for `branch`. Each one has a `README.txt` that names its files:

- `state.json`: the latest state of the pipeline or MR, written on every change.
- `events.jsonl`: every event the watcher reported, and `baseline.json`, the last state this watcher saw.
- `job-logs/<stage>-<name>-<job id>.log` in a pipeline's dir, one file for every job of a watched pipeline. A log is downloaded once, a few seconds after the job ends. A job whose log is not downloaded, because it passed and `--logs` is `failed`, or because it is still running, pending, skipped or manual, has a short placeholder in its place. The placeholder starts with `[glab-cli-extras: log not downloaded]`, says why, and gives the `glab-pipeline inspect … --logs=all` command that downloads it. `state.json` records `log_state` for each job: `downloaded`, `tail` or `placeholder`. On GitLab 19.0 and later a log above 500 KB is read as its last 500 KB only, and the file starts with a `[glab-cli-extras: last …]` line. The log of a downstream job goes to its own pipeline's dir.
- `threads/<created>-<author>-<id>.txt` in an MR's dir, one file per discussion thread, in glab-discussion's format: a header with the full discussion id, the type, the file and line of a diff note, `Resolved:` on resolvable threads and the URL, then one `[<created>] @<username> (note:<id>):` block per note. Bot authors get a `bot-` file name prefix and a `[BOT]` tag, service accounts their display name. A file is rewritten only when the thread changed: a new reply, an edit, a resolve. `.meta.json` maps each discussion id to its file.

- When a watched pipeline finishes, the watcher writes what `glab-pipeline inspect` writes for it: `pipeline.json`, `jobs.json`, `bridges.json`, the lint, test report and downstream files when they apply, and `summary.json` and `summary.txt`. `finished` and the final `watch_ended` carry `summary.txt` in `artifacts`, and `finished` carries the dir in `data.dir`. A later `glab-pipeline inspect` of the same pipeline in the same session finds the files current and makes one request, the ETag probe.

The daemon writes no files. It fetches each log and each changed thread once, keeps it in memory for a while, and hands it to every watcher that asks, so ten watchers of one pipeline cost one download. The watchers write the files.

## Claude Code

Start the watcher as a background Bash task from a Claude Code session:

```bash
glab-watch mr group/project!123 --exclude-events=comments
```

With the default `--events-stream=auto`, a watcher that runs under a Claude Code session which accepts messages sends its events to that session as messages, through the session's messaging socket (`CLAUDE_CODE_MESSAGING_SOCKET`). The session sees each batch as a message from `glab-watch mr group/project!123`:

```text
<glab-cli-extras-watcher resource="mr gitlab.example.com/group/project!123" seq="18-19">
{"kind":"head_pipeline_changed","seq":18,"data":{"pipeline_id":456,"web_url":"https://gitlab.example.com/group/project/-/pipelines/456","watch_command":"glab-watch pipeline https://gitlab.example.com/group/project/-/pipelines/456","status":"running","previous_pipeline_id":455}}
{"kind":"thread_created","seq":19,"data":{"discussion_id":"6a1f…","author":"reviewer-one","resolvable":true,"resolved":false,"notes":1},"artifacts":["/tmp/claude-1000/…/scratchpad/glab-cli-extras/…/merge_requests/123/threads/2026-11-09T09-30-reviewer-one-6a1f….txt"]}
</glab-cli-extras-watcher>
```

- **Paths.** The first message, the one with `watch_started`, also carries `dir="…"` and `changelog="…"`: the resource dir and its `events.jsonl`. Its `watch_started` gives the threads dir relative to that dir, and stdout and `events.jsonl` keep the full paths. Later messages leave the paths out, also after a daemon restart; each event's `artifacts` keep their absolute paths, so a file opens without the first message.
- **Batching.** Events that arrive close together go out as one message: the first event of a burst waits up to 3 s for the rest. A batch above 64 KB is sent as a count per kind; the changelog has the full events.
- **Priority.** `--message-priority=next` (the default) delivers into the session's running turn, at its next tool round. `later` waits until the session is idle and then starts a new turn. `now` is the most urgent.
- **Rate limits.** Each watcher sends as `glab-watch:<pid>`, and keeps below the session's per-sender limit; messages it cannot send yet are merged into the next one.
- **Files.** Under a Claude Code session every file goes to `<session scratchpad>/glab-cli-extras/`, in both stream modes.
- **Lifetime.** A `claude-message` watcher exits when its session is gone: the socket refuses the message, or the session's record is no longer live. Watchers never outlive their session.
- **Subagents.** A watcher started from a subagent's Bash messages the main session. Claude Code has no route to a subagent.
- **Best effort.** The session may drop a message under its own rate limit, or hold it until its user approves it, which a session in bypass-permissions mode does. The changelog always has every event.

`--events-stream=stdout` keeps the events in the task output instead. `--events-stream=claude-message` fails at start when no session accepts messages.

When the session's messaging does not match what the cc-blackbox SDK speaks, for example after a Claude Code update, `auto` uses stdout, and the header says why on a `glab-watch: stream fallback:` line. If that happens while the watcher runs, it prints the same line and sends the rest of the events to stdout.

## Output

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

The header and the other status lines (`ended: …`, errors) always go to stderr, so stdout carries only events. Claude Code puts a background task's stderr into the task output next to its stdout, so an agent sees both. A status line never starts with `{`: in merged output, every line that starts with `{` is an event.

The changelog `events.jsonl` keeps every reported event even when nobody read the stream. It sits in the resource dir next to `state.json` and the logs or thread files; see [Files](#files).

## The daemon

The first watcher starts the daemon, `python -m glab_cli_extras.daemon`, as a detached process. It exits 30 s after its last watcher is gone. You never have to start it.

```bash
glab-watch daemon status      # subscriptions, resources, request counts and budget per host; --json for everything
glab-watch daemon logs -n 100 # the end of the daemon's log
glab-watch daemon stop        # running watchers start a new daemon on their next poll and carry on
```

- **One daemon per user.** It holds an OS lock on `daemon-v3.lock` in its state dir (`$XDG_RUNTIME_DIR/glab-cli-extras/` on Linux, `$TMPDIR/glab-cli-extras/` on macOS, `%LOCALAPPDATA%\glab-cli-extras\run\` on Windows). Watchers that start at the same moment start one daemon, not one each.
- **Leases.** Each watcher polls the daemon every 5 s, which renews its subscriptions. A watcher that dies stops costing anything 20 s later.
- **Restarts.** If the daemon goes away, watchers start a new one and subscribe again. The new daemon compares GitLab's state with the snapshot each watcher kept, so a change made in between is still reported, and nothing is reported twice.
- **Budget.** The daemon paces each host on GitLab's `RateLimit-Remaining` and `RateLimit-Reset` headers and uses at most half of what is left. A host without those headers gets 60 requests a minute. When the budget is short, every poll interval grows by the same factor. A 429 pauses the whole host for its `Retry-After`.
- **Cheap probes.** Pipelines are polled through GitLab's GraphQL ETag cache: an unchanged pipeline answers with a 304 before any query runs. Base intervals: pipeline 5 s while running, MR 30 s (15 s on hosts with a high rate limit), branch pipelines 10 s, branch pushes 30 s, `mrs` 60 s.
- **Sharing.** Watchers of the same resource share one poller. All watched branches of a project share one push-event request.

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

## License

MIT
