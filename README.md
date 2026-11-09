# glab-cli-extras

Extra commands for the [GitLab CLI](https://gitlab.com/gitlab-org/cli) (`glab`). Version 0.1 has one command, `glab-watch`: it watches a merge request, a pipeline, all merge requests of a project, or a branch, and reports every change as one JSON event per line. It is built for coding agents that wait on CI and review, and it works just as well in a terminal or a script.

Every GitLab request goes through `glab api`, so `glab` handles authentication, hosts, OAuth refresh, proxies and certificates. `glab-watch` never reads or stores a token. A shared per-user daemon does the polling, so ten watchers cost about as much API budget as one.

## Install

You need Python 3.11 or later, [uv](https://docs.astral.sh/uv/), and `glab` logged in to your GitLab host (`glab auth status`).

From a local checkout:

```bash
uv tool install --editable /path/to/glab-cli-extras
```

From git:

```bash
uv tool install git+https://github.com/fprochazka/glab-cli-extras
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
- `--events-stream=auto|stdout`: where events go. `auto` is `stdout`.
- `--changelog-dir=<path>`: where the changelog goes. See [Output](#output).
- `--no-daemon`: poll inside this process, with a private budget, without the shared daemon.

`mr` and `pipeline` also take `--logs=failed|all|none`: which finished jobs get their log downloaded. The default is `failed`.

The filter is checked at start. An unknown kind or group, or one the resource never produces, is an error, so a typo does not silently mean "no events". The filter also saves API work: an MR watch without `comments` never fetches threads, and one without `pipeline` never polls the head pipeline or downloads job logs.

## Events

Every event has `kind`, `resource`, `seq`, `observed_at` and `data`, and `artifacts` when it points at files:

```json
{"kind":"job_finished","resource":"pipeline gitlab.example.com/group/project!4567","seq":7,"observed_at":"2026-11-09T10:05:03.120Z","data":{"pipeline_id":4567,"job_id":901,"name":"unit-tests","stage":"test","status":"failed","log_truncated":false},"artifacts":["/home/me/.cache/glab-cli-extras/gitlab.example.com/group/project/jobs/901-unit-tests.log"]}
```

`seq` numbers the events a watcher emits: it starts after the last `seq` in the changelog and only goes up, also when the daemon restarts in between.

Lifecycle events come with every resource and cannot be filtered out:

- `watch_started`: the current state, in `data.summary`.
- `watch_degraded`: polling has trouble: `rate_limited`, `auth`, `not_found` or `error`, with a message. It is sent once per kind of trouble, not on every failed poll.
- `watch_ended`: with a `reason`: `merged`, `closed`, `finished`, `not_found`, `until`, `timeout`, `session_gone`, or the signal.

`mr`:

- `state_changed` (`from`, `to`, and `reopened: true` for closed to opened), `draft_changed`, `title_changed`, `description_changed`
- `pushed`: `from`, `to`, `forced`. A force-push is detected with one `merge_base` request per push.
- `merge_status_changed`: GitLab's transient `checking`, `unchecked` and `preparing` are skipped.
- `approvals_changed`, `reviewers_changed`
- `head_pipeline_changed`, plus every `pipeline` event of the head pipeline, with `pipeline_id` in `data`
- `thread_created`, `thread_updated`, `thread_resolved`, `thread_unresolved`, each with the thread's file in `artifacts`

`pipeline`: `status_changed`, `job_started`, `job_finished` (with the log in `artifacts` when `--logs` asks for it), `downstream_created`, `finished`.

`mrs`: `mr_opened`, `mr_updated`, `mr_merged`, `mr_closed`, `mr_reopened`.

`branch`: `pushed`, `force_pushed`, `created`, `deleted`, `pipeline_created`, `pipeline_finished`. A push whose head commit is a merge commit has `merge_commit: true` and, when GitLab knows it, `merged_mr_iid`.

Groups for `--events` and `--exclude-events`:

| Group | Kinds |
|---|---|
| `status` | `state_changed`, `draft_changed`, `merge_status_changed`, `approvals_changed`, `reviewers_changed`, `title_changed`, `description_changed`, and all `mr_*` |
| `comments` | `thread_*` |
| `push` | `pushed`, `force_pushed`, `created`, `deleted` |
| `pipeline` | `head_pipeline_changed`, `status_changed`, `job_*`, `downstream_created`, `finished`, `pipeline_created`, `pipeline_finished` |
| `lifecycle` | `watch_started`, `watch_degraded`, `watch_ended`; always on |

### Artifacts

Job logs and thread files go to a cache shared by all watchers: `~/.cache/glab-cli-extras/<host>/<project>/` on Linux, `~/Library/Caches/glab-cli-extras/` on macOS, `%LOCALAPPDATA%\glab-cli-extras\cache\` on Windows. Two watchers of the same MR download each thread once.

- A job log is downloaded once, a few seconds after the job ends. On GitLab 19.0 and later a log above 500 KB is read as its last 500 KB only, and the file starts with a `[glab-watch: last …]` line.
- A thread file is `mr-<iid>/threads/<created>-<author>-<id>.md`, in the format glab-discussion uses. It is rewritten only when the thread changed: a new reply, an edit, a resolve.

## Output

On start, every watcher prints a header. Each line starts with `glab-watch: `:

```text
glab-watch: resource: mr gitlab.example.com/group/project!123
glab-watch: stream: stdout (one JSON event per line)
glab-watch: changelog: /tmp/glab-watch-1000/gitlab.example.com/group/project/mr-123/events.jsonl
glab-watch: events: state_changed,draft_changed,pushed,… (always: watch_started,watch_degraded,watch_ended)
glab-watch: ignored: thread_created,thread_updated,thread_resolved,thread_unresolved
glab-watch: logs: failed
glab-watch: daemon: pid 4242, endpoint /run/user/1000/glab-cli-extras/daemon-v1.sock, log /run/user/1000/glab-cli-extras/daemon-v1.log
glab-watch: restart with: glab-watch mr https://gitlab.example.com/group/project/-/merge_requests/123 --events=<kinds|groups> --exclude-events=<kinds|groups> --logs=failed|all|none (groups: status, comments, push, pipeline)
```

The header and the other status lines (`ended: …`, errors) always go to stderr, so stdout carries only events. Claude Code puts a background task's stderr into the task output next to its stdout, so an agent sees both. A status line never starts with `{`: in merged output, every line that starts with `{` is an event.

The changelog keeps every reported event even when nobody read the stream: `<dir>/<host>/<project>/<kind>-<id>/events.jsonl`, plus `state.json` with the last snapshot. The default `<dir>` is `<tempdir>/glab-watch-<uid>`.

## The daemon

The first watcher starts the daemon, `python -m glab_cli_extras.daemon`, as a detached process. It exits 30 s after its last watcher is gone. You never have to start it.

```bash
glab-watch daemon status      # subscriptions, resources, request counts and budget per host; --json for everything
glab-watch daemon logs -n 100 # the end of the daemon's log
glab-watch daemon stop        # running watchers start a new daemon on their next poll and carry on
```

- **One daemon per user.** It holds an OS lock on `daemon-v1.lock` in its state dir (`$XDG_RUNTIME_DIR/glab-cli-extras/` on Linux, `$TMPDIR/glab-cli-extras/` on macOS, `%LOCALAPPDATA%\glab-cli-extras\run\` on Windows). Watchers that start at the same moment start one daemon, not one each.
- **Leases.** Each watcher polls the daemon every 5 s, which renews its subscriptions. A watcher that dies stops costing anything 20 s later.
- **Restarts.** If the daemon goes away, watchers start a new one and subscribe again. The new daemon compares GitLab's state with the snapshot each watcher kept, so a change made in between is still reported, and nothing is reported twice.
- **Budget.** The daemon paces each host on GitLab's `RateLimit-Remaining` and `RateLimit-Reset` headers and uses at most half of what is left. A host without those headers gets 60 requests a minute. When the budget is short, every poll interval grows by the same factor. A 429 pauses the whole host for its `Retry-After`.
- **Cheap probes.** Pipelines are polled through GitLab's GraphQL ETag cache: an unchanged pipeline answers with a 304 before any query runs. Base intervals: pipeline 5 s while running, MR 30 s (15 s on hosts with a high rate limit), branch pipelines 10 s, branch pushes 30 s, `mrs` 60 s.
- **Sharing.** Watchers of the same resource share one poller. All watched branches of a project share one push-event request.

## Platform support

- Linux and macOS: everything. The daemon listens on a Unix socket.
- Windows: the daemon and the stdout stream. The daemon listens on a loopback TCP port, and every request carries a random token from a file only the user can read.

## Limitations

- **One glab process per request.** Starting `glab` costs tens of milliseconds. At these request rates, a few per second at most, that does not matter.
- **The free ETag 304 is not documented by GitLab.** If GitLab starts to count those requests, pipeline watching gets slower under the budget, not more expensive.
- **Branch pipeline probes are cheap only on quiet projects.** GitLab touches the ETag key of a project's pipelines on any pipeline change in the project. In a busy monorepo nearly every 10 s probe is a full request.
- **`mr_updated` is best effort.** GitLab does not move an MR's `updated_at` when it was touched less than a minute before, so a second change within that minute shows up only with the next change.
- **Job logs of large jobs need GitLab 19.0** for the tail-only download. Older versions download the whole log.
- **Thread files have no bot marker** and use usernames, not display names, because that would cost a user lookup per author.
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

The tests need no network and no glab login. A fake `glab` (`tests/fakeglab.py`) answers with the response shapes recorded from a GitLab 19.5 instance, in process for the pollers and as a real executable on `PATH` for the daemon tests, which start real daemons in temporary dirs.

## License

MIT
