# Changelog

## Unreleased

Fixed:

- `glab-watch pipeline` and `glab-pipeline inspect --pipeline-url` find child pipelines, the ones a `trigger:` job starts in the same project. Before, both said the pipeline was not found, and `glab-watch` ended with `not_found`. GitLab's GraphQL lookup by pipeline ID leaves child pipelines out, so when it answers no pipeline, the IID now comes from `GET /projects/:id/pipelines/:pipeline_id` and the tree is fetched by IID. That costs two more requests on the first poll of a child pipeline; later polls of the watch ask by IID directly. Other pipelines cost no extra request.

Changed:

- When the MR or the pipeline had already ended when `glab-watch` started, its `watch_ended` event has `already_ended: true`, and the status line on stderr says so, for example `ended: finished (already finished when the watch started)` or `ended: merged (already merged when the watch started)`. The exit code stays 0. A watch whose earlier snapshot saw the target still open or running, such as one that subscribed again after a daemon restart, has no such flag.

## 0.9.0 (2026-11-12)

Added:

- `glab-discussion edit --attach <file>`, repeatable, adds files to a note or a draft with the same checks and placement as `write --attach`. With `--body`, the links go into the new text. Without `--body`, the command reads the current text from GitLab and adds the links to it, so `--body` is optional when `--attach` is given. The `discussion.quick_actions` setting applies to the text that is sent. A failed upload leaves the comment unchanged.

Changed:

- The Bash guard (`glab-cli-extras-cc-hook`) allows `glab api projects/…/ci/lint` that sends content to lint: with `-X`/`--method`, `--input`, `-f`, `-F`, `--field` or `--raw-field`. It still denies a plain GET of that endpoint, which reads the lint result of a ref; `glab-pipeline inspect --with-merged-ci-config` covers that.
- `glab-discussion write --attach <file>` takes any file, not only PNG, JPEG, GIF and WebP images, and adds the Markdown GitLab returns for it. Images, video, audio and SVG render inline; any other file, such as a log, is a download link. The command checks only that the file exists and can be read. When GitLab refuses a file over the instance's maximum attachment size, the error says so and nothing is posted.

## 0.8.0 (2026-11-11)

Changed:

- On GitLab Community Edition, the commands that take the MR snapshot ask for it without `approvalsLeft` on the first try. Since 0.7.2 every run of `glab-discussion read`, `glab-mr-status` and `glab-pipeline inspect --mr-url` first sent a query that GitLab rejected. The commands now ask each host for its version and edition once a day and keep the answer in `~/.cache/glab-cli-extras/<host>/capabilities.json` (`$XDG_CACHE_HOME`, `%LOCALAPPDATA%` on Windows, or `GLAB_CLI_EXTRAS_CACHE_DIR`). That is one extra request per host per day. The daemon keeps the answer in memory and still writes no files. If GitLab rejects the field anyway, or the check gets no answer, the query is asked again without the field, as in 0.7.2, and the cache records the field as missing. See [The capability cache](README.md#the-capability-cache).

## 0.7.2 (2026-11-11)

Fixed:

- `glab-discussion read`, `glab-watch mr` and the other commands that take the MR snapshot work on GitLab instances whose GraphQL schema has no `MergeRequest.approvalsLeft`, such as GitLab Free. They failed with `GraphQL error: Field 'approvalsLeft' doesn't exist on type 'MergeRequest'`. The snapshot is now asked again without the field, and that host is asked without it from then on; the approvals left are unknown there.

## 0.7.1 (2026-11-11)

Fixed:

- Under a Claude Code session on macOS, `glab-watch` sends its events to the session instead of to stdout. It needs cc-blackbox 0.5.1, which no longer treats every macOS session as not running when Claude Code writes `"pidDomain": "darwin"` into the session record.
- `glab-watch` with `--events-stream=auto` says why it sends events to stdout also when `CLAUDE_CODE_MESSAGING_SOCKET` is set but no live session record was found. Before, it fell back to stdout without a `stream fallback:` line.

## 0.7.0 (2026-11-10)

Added:

- `glab-discussion write --note` posts a plain comment through the MR notes endpoint instead of starting a resolvable thread. It is never a draft, also under `GLAB_DISCUSSION_WRITE_AS_DRAFT=true`. With `--reply-to`, `--file`, `--new-line`, `--old-line`, `--commit`, `--draft` or `--resolve` it is a usage error, exit 2.
- `glab-discussion write --attach <image>`, repeatable, uploads a PNG, JPEG, GIF or WebP image to the MR's project (`POST /projects/:id/uploads`) and shows it in the comment: after the body text and a blank line, one image per line, in the order given, and before a signature line such as `<!-- plugin:command -->` when the body ends with one. It works for threads, replies, diff notes, `--note` and drafts. A file that is missing, or that is not one of these images by both its extension and its first bytes, stops the command before any request. The upload needs a glab whose `api` command has `--form`.
- Settings that restrict the commands for unattended agents, read from `/etc/glab-cli-extras/config.toml`, the user's `~/.config/glab-cli-extras/config.toml` (`%PROGRAMDATA%` and `%APPDATA%` on Windows), the file `GLAB_CLI_EXTRAS_CONFIG` names, and one `GLAB_CLI_EXTRAS_<SECTION>_<KEY>` environment variable per setting. A restriction that any source sets applies, and no other source loosens it. An unknown table, key or value is an error, exit 1. See [Configuration](README.md#configuration).
- `discussion.quick_actions = "reject"` makes `glab-discussion write`, `edit` and `drafts publish --body` refuse a body with a line that starts with `/word` outside a closed fenced code block, with exit 1 before any request. The review that `drafts publish` submits with `/submit_review` is not affected.
- `discussion.drafts = "disabled"` removes the `glab-discussion drafts` command and the `write --draft`, `--no-draft` and `--resolve` options from the parser, so they are missing from `--help` and using one is a usage error, exit 2. `edit` and `delete` take only `note:<id>`. No command writes a draft or submits a review then, and `GLAB_DISCUSSION_WRITE_AS_DRAFT=true` stops `write` with exit 1.

Changed:

- `glab-watch --events-stream=claude-message` wraps each message in a `<glab-watcher>` tag instead of `<glab-cli-extras-watcher>`. A watcher started before the upgrade keeps sending the old tag until it ends.
- `glab-discussion write --draft --reply-to` explains GitLab's refusal of a second draft reply in a thread where you already have one: GitLab keeps one draft reply per user and thread, and answers `400` with `author_id has already been taken`, which the command printed as it came. The error names the existing draft, and says to edit it or to delete it and write the reply again.

## 0.6.0 (2026-11-10)

Added:

- `glab-search-code` runs a GitLab code search and downloads every file that matched into a new dir per run, under the base dir the other commands use: `files/<project path>/<file path>`, `metadata.json`, `hits.json` with the matched lines, and `download.log`. It replaces the standalone glab-search-code tool; run `uv tool uninstall glab-search-code` before you install this release, because both install the same executable. What changed for its users:
  - The run's dir is `<base>/<host>/-/search/<UTC time>-<query>-<hash>/` instead of `<temp dir>/glab-code-search-<time>/`, and `--output-dir` names it. Take it from the `Output directory:` line.
  - Files keep their repository paths instead of a flat `project__path__file` name. A file that matched on several lines is downloaded once.
  - `--hostname` is optional: the default is the host of the current directory's repository. glab's `config.yml` is no longer read.
  - `--max-retries` is the number of attempts per request, default 5 instead of 3. Besides a rate limit, it also retries a 5xx and a network error, and waits for `Retry-After`.
  - `--project` and `--group` search one project or one group. A search across a group or the instance needs Advanced search or Exact code search; a 400 for that says to pass `--project`.
  - Progress goes to stderr; stdout carries only the summary, which still ends with the `Output directory:` and `Metadata file:` lines. `metadata.json` entries add `url` and `hits`, the start lines of the matches.
  - The query and the ref are URL-encoded, so `&`, `#`, `+` and spaces in them work. A search that reaches 10,000 hits says it stopped.
  - `--workers` outside 1 to 50 is a usage error, exit 2. `--version` prints `glab-cli-extras <version>`.
- The `glab:glab-search-code` skill teaches `glab-search-code`: the scope flags, the Advanced or Exact code search a group or instance search needs, taking the paths from the last two lines, and reading `hits.json` and `metadata.json` with `jq` before `rg` over the files. The `glab:glab` skill sends a code search across projects to it.
- The glab plugin's SessionStart check reports a glab-cli-extras install that lacks `glab-search-code`, and a `glab-search-code` on `PATH` that is still the standalone tool, with the command that fixes it: `uv tool uninstall glab-search-code && uv tool install --force glab-cli-extras`.

## 0.5.1 (2026-11-10)

Changed:

- The `glab:glab-watch` skill says what to do when a watcher ends early, and how to run a `watch_command`, so the skills that start watchers need not say it. A watcher that ended for a reason other than its resource's end or an end you asked for is started again with all its options, and the user is told; `not_found` goes to the user instead, and a second end with the same reason within minutes is reported as a failure, not restarted again. A background task that ends without `watch_ended` ended for an unknown reason. The `watch_command` of an event starts another watcher, so it runs in the background, never in the foreground.

## 0.5.0 (2026-11-10)

Added:

- `glab-watch mr` reports `target_branch_changed`, `assignees_changed`, `labels_changed` and `blocking_threads_resolved_changed` in the `status` group, and `external_status_changed` in the `pipeline` group: a commit status on the head that no job of the head pipeline set, the one `glab-mr-status` prints as `failed external status:`. The first four come with the MR's snapshot query at no extra cost. The external statuses cost one request per 100 statuses of the head commit, made when the head, the head pipeline or an unfinished external status moved, and otherwise about every five minutes. A watch filtered to other groups fetches none. The `watch_started` summary adds `assignees`, `labels`, `blocking_threads_resolved` and `external_statuses`.

Fixed:

- `glab-pipeline inspect --with-merged-ci-config` runs the two-step lint, the one that resolves `include:` against the source branch, also when the pipeline is given by `--pipeline-url` or `--pipeline-id`. The branch comes from the pipeline's own ref, and for a merge request pipeline from its MR's source branch, at one extra request. Before, such a pipeline fell back to the cheap lint with a warning, and `--pipeline-id` alone linted the current branch's MR even for another branch's pipeline.
- `glab-discussion` and `glab-pipeline inspect` accept an `--mr-url` copied from the browser: with a tab such as `/diffs`, `/commits`, `/pipelines` or `/overview`, a query string, or an anchor such as `#note_<id>`, as `glab-mr-status` and `glab-watch` already did. The URL is reduced to the MR's own URL, so the `URL:` lines of the thread files and the links the write commands print carry one anchor each.

Changed:

- The glab plugin's SessionStart check says that the Bash guard is inactive, and allows every Bash call, while glab-cli-extras or its `glab-cli-extras-cc-hook` command is not installed. The plugin README says the same. A note on 0.3.0: the guard that release added has always allowed every call without glab-cli-extras, on purpose, so a missing install never blocks the session's work. The guards of the old `glab-pipeline` and `glab-discussion` plugins fell back to text patterns in that case; this one has no fallback.
- The `glab:glab-discussion` skill says why `drafts publish` can publish nothing: GitLab did not add you as a reviewer, which a verdict needs, or did not remove your earlier verdict. It says what the command does about each and what to tell the user.
- The daemon's protocol version is 4, so a running 0.4 daemon, which knows none of the new event kinds, is left to stop on its own and a new one starts next to it. Its socket and lock are `daemon-v4.*`.

## 0.4.2 (2026-11-10)

Changed:

- Every skill and command of the `glab` plugin is named `glab:<name>` in its front matter. Claude Code made a bare name such as `name: glab-watch` an alias, so `/glab-watch` and a `Skill` call with `glab-watch` worked, and the slash menu showed `/glab:glab-watch (glab-watch)`. The bare names no longer work: use `/glab:glab-watch`, `/glab:overview`, `glab:glab-pipeline` and the other `glab:` names, also in `Skill(…)` permission rules. The skills and the plugin README refer to each other by those names.
- The `glab:glab-watch` skill says how watchers and subagents work together: the main session starts and owns the watchers, and forwards the events that concern a subagent's delegated work to it with SendMessage, as the event lines and the file paths they name. A subagent starts no watcher of its own, because its messages would reach the main session anyway.

## 0.4.1 (2026-11-10)

Changed:

- The `glab:glab` skill's section on long bodies also covers options that take no file: pass the file's content as `--<option> "$(cat <file>)"`.

Removed:

- The `glab:glab-watch` skill's `references/glab-mr-status.md` and `references/judging-review-state.md`, and the pointers to them from the skills and the `/glab:overview`, `/glab:comments` and `/glab:pipeline` commands. `glab-mr-status --help` now covers its output: the files it writes into the MR's dir, the `state:`, `discussions:`, `pipeline dump:` and `failed external status:` lines that name the paths and the changed thread files, and that only the listed thread files need reading. The plugin no longer carries rules for judging pipelines, AI reviewer verdicts and merge blockers.

## 0.4.0 (2026-11-10)

Breaking changes:

- The `glab:mr-status` skill is removed. It described one command that the `/glab:overview`, `/glab:comments` and `/glab:pipeline` commands already run. Its content moved into two references of the `glab:glab-watch` skill, which the commands read too: `references/glab-mr-status.md` says how to run `glab-mr-status` and read its change lines, files and exit codes, and `references/judging-review-state.md` holds the rules for judging pipelines, AI reviewer verdicts, human review and merge blockers. A prompt or plugin that loads `glab:mr-status` by name should load `glab:glab-watch` instead, or run `glab-mr-status` directly. The format for reporting a set of MRs is dropped.
- The `glab:glab` skill covers only the mistakes glab's own `--help` does not prevent: pagination that silently returns one page, long bodies passed inline instead of through a file, and which glab-cli-extras command replaces a raw glab call. The reference of glab commands and flags is gone; run `glab <command> --help`.

## 0.3.1 (2026-11-10)

Fixed:

- The `glab:glab-watch` skill now explains why agents leave `--events-stream` at its default: with `auto`, events arrive in the session as messages; with `stdout`, the agent would have to read the task output or `events.jsonl` itself to notice a change. The old wording claimed nobody reads stdout events.

## 0.3.0 (2026-11-09)

Breaking changes:

- Python 3.12 or later is required. bash-classify, which the new Claude Code hook uses as a library, requires it. Install with a newer Python: `uv tool install --python 3.12 glab-cli-extras`.

Added:

- `glab-mr-status`, which reads merge requests once and reports what changed since its last read: one `<ID>: <detail>` line per change, the threads and the head pipeline's dump, and a scorecard line. It replaces the `mr-state.py` script of the `glab` Claude Code plugin with the same output, change ids and exit codes, and keeps its baseline apart from a running watcher's. See [glab-mr-status](README.md#glab-mr-status).
- `glab-watch mr --quiet-after=<duration>`, repeatable, reports `quiet` when the head pipeline succeeded on the current head and nothing else was reported for that long. `glab-watch mr` and `glab-watch pipeline --stuck-after=<duration>` report `pipeline_stuck` for a pipeline still running or pending that long. Both are computed from what the watcher already receives and cost no request.
- `glab-watch --idle-after=<duration>` on every resource ends the watch with `watch_ended` reason `idle` when nothing was reported for that long.
- `glab-cli-extras-cc-hook`, a Claude Code PreToolUse hook that denies inspecting pipelines with `glab ci view|get|trace` or the raw API, and reading or writing MR discussions with `glab api`, `glab mr view --comments` or `glab mr note`, and names the command and skill to use instead. It replaces the bash hooks of the glab-pipeline and glab-discussion plugins, with their rules and test cases. See [The Claude Code hook](README.md#the-claude-code-hook).
- The repository is a Claude Code marketplace, `fprochazka-glab-cli-extras`, with the `glab` plugin: the `glab`, `glab-watch`, `glab-pipeline`, `glab-discussion` and `mr-status` skills, the `/glab:overview`, `/glab:comments` and `/glab:pipeline` commands, the Bash guard and a session check of the install. It carries the package's version, and replaces the `glab` plugin of claude-code-plugins and the glab-pipeline and glab-discussion plugins. See [Claude Code plugin](README.md#claude-code-plugin).
- `glab-pipeline --version` and `glab-discussion --version` print `glab-cli-extras <version>`.

Changed:

- An MR watch reads the head pipeline's SHA with its snapshot, so `quiet` can tell a green pipeline on the current head from the one GitLab keeps after a push.
- The header of a watch lists its thresholds on a `thresholds:` line, and `--idle-after` on the `ends:` line.

Deprecated:

- The standalone glab-pipeline and glab-discussion packages. When one of them is still installed next to glab-cli-extras, its command prints one line on stderr before it runs, for example `glab-pipeline: this package is deprecated; glab-cli-extras provides the same command. Run: uv tool uninstall glab-pipeline && uv tool install glab-cli-extras`. Stdout and the exit code stay the same.

## 0.2.0 (2026-11-09)

Breaking changes for `glab-watch`, with what to do:

- `glab-watch mr` no longer takes `--logs`, and passing it is an error. An MR watch reports its head pipeline at the pipeline level only. Drop the option, and run the `watch_command` of a pipeline event to follow the jobs and get their logs.
- `--changelog-dir` is replaced by `--base-dir`, which holds every file of the watch, not only the changelog.
- Files moved. Job logs and thread files were in `~/.cache/glab-cli-extras/`, and the changelog in `<scratchpad>/glab-watch/`. Everything now sits in one dir per resource under `<scratchpad>/glab-cli-extras/`, or `<temp dir>/glab-cli-extras-<uid>/` outside Claude Code. Take paths from the events and the startup header, never from a hard-coded pattern.
- The watcher's own last snapshot is `baseline.json`; `state.json` is now the shared latest state of the resource.
- Thread files are `.txt` in glab-discussion's format instead of `.md`.
- The daemon protocol is version 3. A 0.1 daemon keeps serving its own watchers until it is idle; it does not mix with 0.2.

Changes an agent that reads glab-watch output sees, and why each one leaves it as well off or better:

- One dir per resource holds `state.json`, the changelog, the job logs or thread files, and a `README.txt` that names them. The startup header gains `dir:` and `state:` lines, and `watch_started` names the dir. Before, an agent looked in two trees; now any path of an event leads to the rest.
- Thread files are the files `glab-discussion read` writes, byte for byte, with `[BOT]` tags and service-account display names. The skills that parse glab-discussion's format read them unchanged, and a `read` after a watch fetches nothing.
- Every job of a watched pipeline has a file at its log's path. A log that was not downloaded is a placeholder that says why and gives the `glab-pipeline inspect … --logs=all` command that downloads it. Before, the file was missing, and an agent could not tell "not downloaded" from "no log".
- A finished pipeline watch leaves the dump `glab-pipeline inspect` writes, with `summary.txt` in the `artifacts` of `finished` and `watch_ended`. An agent reads the summary instead of running another command.
- An MR watch reports `head_pipeline_changed`, `pipeline_status_changed` and `pipeline_finished` with the failed job names, each with the `glab-watch pipeline` command, instead of every job event of the head pipeline. The MR's own events no longer drown in job events, and the job detail is one command away.
- `approvals_changed` is no longer sent when GitLab did not return the approvals. Before, a missing field read as "every approval was removed".
- `thread_unresolved` within two polls of a push has `after_push: true`, because GitLab can show a resolved thread unresolved for a moment after a force-push. The event is still sent.
- `merge_status_changed` is no longer sent once the MR is merged or closed; `state_changed` already says it.
- A message to the Claude Code session names the resource dir and the changelog only on the first message, the one with `watch_started`. Later messages carry `resource` and `seq`, and every artifact path stays absolute, so a file opens without the first message. stdout and `events.jsonl` are unchanged.
- `watch_degraded` with the reason `daemon_mismatch` says when the running daemon is older than the watcher, and how to replace it.

Added:

- `glab-pipeline inspect` and `glab-discussion`, the commands of the glab-pipeline and glab-discussion packages, with their options and output. They fetch without the daemon and write into the same dirs as `glab-watch`, and a rerun fetches only what changed. Their own changes are in [glab-pipeline](README.md#glab-pipeline) and [glab-discussion](README.md#glab-discussion): the dump dirs moved into the base dir, a partial set of context flags is an error, failed jobs' logs are the default, and the write commands check their input before calling GitLab.

Fixed:

- Every command writes UTF-8 on Windows too. `glab-watch` stopped with an encoding error on an event with an emoji, such as an MR title, because Python writes a pipe in the console code page there.
- A daemon state dir too long for a Unix socket path (104 bytes on macOS, 108 on Linux) is reported at once with the reason. The daemon used to die without a log line, and the watcher waited 10 s for it.

## 0.1.0 (2026-11-09)

The first release, with `glab-watch`.
