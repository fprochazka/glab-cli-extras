# glab-mr-status

`glab-mr-status` reads a merge request once: the MR object, the newest note, the approvals, the threads, the head pipeline's dump and the external commit statuses. It remembers what it saw and prints what changed since its last read. To be told when something changes, start `glab-watch mr` instead; never call `glab-mr-status` in a loop.

## Running it

```bash
glab-mr-status                                  # the current branch's MR, full read
glab-mr-status <mr url> [<mr url> …]            # a set, across repos and hosts
glab-mr-status --comments                       # the threads only; --pipeline the pipeline dump only
glab-mr-status --reset <mr url>                 # forget this reader's last read and start a new baseline
```

- **Name an MR by its URL** when it is not the current branch's. A set can span repos and hosts, and outside a checkout `glab` falls back to its default host and returns a wrong answer instead of an error. `group/project!123` works too.
- **A full read** fetches everything: `--all` is the default, `--comments` and `--pipeline` narrow it. The first read of an MR establishes a baseline and says so; every later read prints the changes since the read before. An unchanged MR costs a handful of requests, because the threads and the pipeline dump are fetched again only where they changed.
- **Exit codes**: 0 for a baseline or a read, 5 when no MR could be read, 1 for an error such as no MR on the current branch, 2 for a usage error.

## The files

Everything goes into the MR's dir, `<base>/<host>/<project>/-/merge_requests/<iid>/`, which the last line of the output names (`state:`):

- `status-baseline.json`: this reader's last read.
- `status-events.log`: every change it printed, in UTC.
- `mr.json`: the raw MR object, with the description.
- `external-statuses.json`: the commit statuses posted from outside the pipeline.
- `threads/`: one file per thread, in the format the glab-discussion skill describes.

The pipeline dump is in the pipeline's own dir, which the `pipeline dump:` line names. Read paths from the output, never build them. The command prints, flushes, and only then stores the read, so a run killed in between reports the same change again. A running `glab-watch` keeps its own baseline: a read never swallows a change the watcher has not reported yet, and the reverse.

## The output

Per MR: a `== host/project!iid` header, one line per change, the detail paths, then a `scorecard:` line. Each change line starts with a fixed id. The last column names the glab-watch event that reports the same change, so a session that holds both can match one to the other:

| Id | Meaning | glab-watch |
|---|---|---|
| `STATE_CHANGED` | opened, merged, closed, locked | `state_changed` |
| `DRAFT_CHANGED` | draft or ready | `draft_changed` |
| `HEAD_CHANGED` | a push, with the full SHAs; "rebase likely" when the behind count dropped to zero at the same time | `pushed` |
| `TARGET_CHANGED`, `TITLE_CHANGED` | the target branch or the title changed; a target change means the MR was re-stacked | `title_changed` |
| `PIPELINE_CHANGED` | a new pipeline or a status change; says when it ran on a superseded base | `head_pipeline_changed`, `pipeline_status_changed`, `pipeline_finished` |
| `BEHIND_CHANGED` | commits behind the target branch | none |
| `MERGE_STATUS_CHANGED` | mergeability or conflicts, transient values already filtered out | `merge_status_changed` |
| `NOTES_CHANGED` | a new note, or an edit or resolve of an existing one, with the author; on a count-only probe, the count or an unexplained `updated_at` | `thread_created`, `thread_updated`, `thread_resolved`, `thread_unresolved` |
| `DISCUSSIONS_RESOLVED_CHANGED` | the all-blocking-threads-resolved flag flipped | `thread_resolved`, `thread_unresolved` |
| `REVIEWERS_CHANGED`, `ASSIGNEES_CHANGED`, `LABELS_CHANGED` | field diffs; a reviewer assignment shows only here, never as a note | `reviewers_changed` |
| `APPROVALS_CHANGED` | who approved, how many approvals are left | `approvals_changed` |
| `READ_FAILED` | one MR could not be read; the others were | `watch_degraded` |

Then the details:

- `discussions:` names the threads dir and lists the files this read rewrote as `updated:`, `new:` and `deleted:`. A thread file is rewritten only when its content changed, so **read only the files it lists**, or the files newer than your last pass.
- `pipeline dump:` names the pipeline's dir and lists each failed job with its stage and reason. Open `job-logs/<failed-job>.log`, `test-report.json` or `lint.json` only when the summary points at them.
- `failed external status:` is a commit status posted by something outside the pipeline, with its URL.

The scorecard says `on an older head` when the pipeline's SHA is not the MR head. How to judge the pipeline, the verdicts and the reviewers is in `judging-review-state.md` next to this file.
