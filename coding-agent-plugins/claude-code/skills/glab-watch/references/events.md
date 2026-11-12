# glab-watch events

Every event has `kind`, `resource`, `seq`, `observed_at` and `data`, and `artifacts` (absolute paths) when it points at files. `seq` only goes up, also across daemon restarts.

## Lifecycle, every resource

| Kind | Data | React |
|------|------|-------|
| `watch_started` | `summary`: the current state | Read the summary as the starting point. Changes before it are not events. |
| `watch_degraded` | `reason`, `message` | `auth`: tell the user to run `glab auth status`. `rate_limited`: nothing to do, polling slows down. `not_found`: check the ref. `error`: report the message. `daemon_mismatch`: run `glab-watch daemon stop`; the watchers start a current daemon. Each reason comes once, not per failed poll. |
| `watch_ended` | `reason`: `merged`, `closed`, `finished`, `not_found`, `until`, `timeout`, `idle`, `session_gone`, or the signal; `already_ended: true` when the MR or the pipeline had already ended when the watch started | The watcher has exited. The skill says when to start it again. With `already_ended`, nothing happened while watching: the end is the state the target was already in. |

## `glab-watch mr`

| Kind | Group | Data | React |
|------|-------|------|-------|
| `state_changed` | status | `from`, `to`, `reopened` | Merged or closed ends the watch. |
| `draft_changed` | status | `draft` | Evaluate the caller's handover rule, if any. |
| `title_changed` | status | `from`, `to` | |
| `target_branch_changed` | status | `from`, `to` | The MR was re-targeted. A watch on the old target branch watches the wrong branch now; restart it on `to`. |
| `description_changed` | status | | Read the MR description again if it matters. |
| `merge_status_changed` | status | `from`, `to` | GitLab's transient values are already skipped. |
| `approvals_changed` | status | `approved`, `approvals_left`, `approved_by`, `added`, `removed` | Tell a human approval from an AI approver's. |
| `reviewers_changed` | status | `reviewers`, `added`, `removed` | Evaluate the handover rule, if any. |
| `assignees_changed` | status | `assignees`, `added`, `removed` | Evaluate the handover rule, if any. |
| `labels_changed` | status | `labels`, `added`, `removed` | Act on a label only if the caller gave it a meaning. |
| `blocking_threads_resolved_changed` | status | `resolved` | `false`: an unresolved thread now blocks the merge. Also `true` in a project that does not require resolved threads. |
| `pushed` | push | `from`, `to`, `forced`, `forced_reason` | Compare `to` with your own push. Verdicts and pipelines on `from` are stale. |
| `head_pipeline_changed` | pipeline | `pipeline_id`, `status`, `previous_pipeline_id`, `web_url`, `watch_command` | A new pipeline for the head. For its jobs, start `watch_command` in the background, if needed. |
| `pipeline_status_changed` | pipeline | `pipeline_id`, `from`, `to`, `web_url`, `watch_command` | |
| `external_status_changed` | pipeline | `name`, `from`, `to`, `allow_failure`, `description`, `target_url` | A commit status on the head that the head pipeline did not set, such as a quality gate outside the CI. `from` is `null` for a new one. Failed: open `target_url`; `glab-pipeline` does not see it. |
| `pipeline_finished` | pipeline | `pipeline_id`, `status`, `failed_jobs`, `web_url`, `watch_command` | Failed: inspect with `glab-pipeline inspect` and read `summary.txt` first. |
| `thread_created` | comments | `discussion_id`, `author`, `resolvable`, `resolved`, `notes`; the thread file in `artifacts` | Read the file. |
| `thread_updated` | comments | `discussion_id`, `new_notes`, `edited_notes`, `removed_notes`, `last_author`, `resolved`; the file | Read the file. |
| `thread_resolved` | comments | `discussion_id`; the file | |
| `thread_unresolved` | comments | `discussion_id`, `after_push`; the file | With `after_push: true`, read the thread again before acting: it may be a moment of GitLab recomputing after a force-push. |
| `quiet` | status | `after`, `head_sha`, `pipeline_id` | Only with `--quiet-after`. The head is green and nothing else was reported for `after`. |
| `pipeline_stuck` | pipeline | `after`, `pipeline_id`, `status`, `web_url`, `watch_command` | Only with `--stuck-after`. Look at the running jobs with `glab-pipeline inspect`, or start `watch_command` in the background. |

An MR watch reports its head pipeline at the pipeline level only; job events and job logs need `glab-watch pipeline`.

## `glab-watch pipeline`

| Kind | Data | React |
|------|------|-------|
| `status_changed` | `pipeline_id`, `from`, `to` | |
| `job_started` | `pipeline_id`, `job_id`, `name`, `stage`, `status` | |
| `job_finished` | `pipeline_id`, `job_id`, `name`, `stage`, `status`, `log_truncated`; the log in `artifacts` | Failed: read the log. A placeholder says how to download a log that was not fetched. |
| `downstream_created` | `pipeline_id`, `downstream_pipeline_id`, `project`, `status`, `web_url` | Its jobs are reported too, one level deep. |
| `finished` | `pipeline_id`, `project`, `status`, `web_url`, `dir`; `summary.txt` in `artifacts` | Read `summary.txt`. The dump `glab-pipeline inspect` writes is in `dir`. |
| `pipeline_stuck` | `after`, `pipeline_id`, `status`, `web_url`, `running_jobs` | Only with `--stuck-after`. |

`--logs=failed|all|none` decides which finished jobs get their log downloaded; the default is `failed`.

## `glab-watch mrs`

`mr_opened`, `mr_updated`, `mr_merged`, `mr_closed`, `mr_reopened`, all in the `status` group. When the list shows what moved, `changes` maps a field to `[old, new]` for `title`, `draft`, `target_branch`, `sha`, `detailed_merge_status`, `labels`, `assignees` and `reviewers`.

## `glab-watch branch`

`pushed` and `force_pushed` (a merge commit has `merge_commit: true` and, when GitLab knows it, `merged_mr_iid`), `created`, `deleted` (group `push`), `pipeline_created`, `pipeline_finished` (group `pipeline`).
