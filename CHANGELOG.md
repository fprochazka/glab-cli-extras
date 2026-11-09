# Changelog

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
