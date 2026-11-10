# Changelog

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
