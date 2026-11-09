---
name: mr-status
description: Efficiently determine the review-and-merge status of one merge request or a set of them — draft, rebase, pipeline, approvals, AI-reviewer verdicts, human threads, what changed since the last read, and what actually blocks the merge. Use when asked "what is the status of MR X", "which MRs are ready to merge", "is this MR green", "who still has to review this", or when a command needs the current review state before it acts. The sdlc and code-review commands invoke this skill by name, and the glab-watch skill uses it to classify what a watcher reports.
trigger-keywords: mr status, merge request status, review queue, mr overview, is the mr ready, review state, what blocks the merge
allowed-tools: ["Bash(glab-mr-status:*)", "Bash(glab-discussion read:*)", "Bash(glab-pipeline inspect:*)", "Bash(glab api:*)", "Bash(glab mr view:*)", "Bash(git fetch:*)", "Bash(git rev-list:*)", "Bash(git diff:*)"]
---

# mr-status

One command reads a merge request; you judge it. `glab-mr-status` fetches the MR object, the newest note, the approvals, the threads, the head pipeline's dump and the external commit statuses, remembers what it saw, and prints what changed since its last read. This skill says how to run it, how to read what it prints, and the judgment calls the command cannot make. It covers GitLab. `glab-mr-status` comes with glab-cli-extras (`uv tool install glab-cli-extras`).

## Running glab-mr-status

```bash
glab-mr-status                                  # the current branch's MR, full read
glab-mr-status <mr url> [<mr url> …]            # a set, across repos and hosts
glab-mr-status --comments                       # the threads only; --pipeline the pipeline dump only
glab-mr-status --reset <mr url>                 # forget this reader's last read and start a new baseline
```

- **Always identify an MR by its URL** when it is not the current branch's. A set can span repos and hosts, and `glab` outside a checkout falls back to its default host and returns a wrong answer instead of an error. `group/project!123` works too.
- **A full read** fetches everything: `--all` is the default, `--comments` and `--pipeline` narrow it. The first read of an MR establishes a baseline and says so; every later read prints the changes since the read before. An unchanged MR costs a handful of requests: the threads and the pipeline dump are fetched again only where they changed.
- **There is no wait mode.** To be told when something changes, start a watcher; see the glab-watch skill. Never call `glab-mr-status` in a loop.
- **Files.** Everything goes into the MR's dir, `<base>/<host>/<project>/-/merge_requests/<iid>/`, which the last line of the output names (`state:`): `status-baseline.json` (this reader's last read), `status-events.log` (every change it printed, UTC), `mr.json` (the raw MR object), `external-statuses.json`, and the `threads/` dir. The pipeline dump is in the pipeline's own dir, which the `pipeline dump:` line names. Read paths from the output, never build them. The command prints, flushes, and only then stores the read, so a run killed in between reports the same change again. A running `glab-watch` keeps its own baseline, so a read never swallows a change the watcher has not reported, and the reverse.
- **Exit codes**: 0 for a baseline or a read, 5 when no MR could be read, 1 for an error such as no MR on the current branch.

## Reading the output

Per MR, the command prints a `== host/project!iid` header, then one line per change, then the detail paths, then a `scorecard:` line. Change lines carry a fixed id. The last column names the glab-watch event that reports the same change, so a session holding both reads one through the other:

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
| `REVIEWERS_CHANGED`, `ASSIGNEES_CHANGED`, `LABELS_CHANGED` | field diffs; reviewer assignment is visible only here, never as a note | `reviewers_changed` |
| `APPROVALS_CHANGED` | who approved, how many approvals are left | `approvals_changed` |
| `READ_FAILED` | one MR could not be read; the others were. The command exits non-zero (`5`) only when nothing could be read at all | `watch_degraded` |

Then the details. `discussions:` names the threads dir, one `.txt` per thread, and lists the files the read rewrote as `updated:`, `new:`, `deleted:`. A thread file is rewritten only when its content changed, so **read only the files it lists, or the files newer than your last pass**. `pipeline dump:` names the pipeline's dir and lists each failed job with its stage and reason; open only `job-logs/<failed-job>.log`, `test-report.json`, or `lint.json` when the summary points at them. `failed external status:` is a commit status posted by something outside the pipeline, with its URL.

A thread file starts with a header: `Discussion: <full id>`, `Type: General | DiffNote`, `File:` and `Line:` for a DiffNote, `Resolved: yes | no` only on a resolvable thread, then one `[<created_at>] @<username> [BOT] (note:<id>):` block per note. The file shows creation times only, so an edited note keeps its old time inside the file; the listing is what tells you it moved. The read also holds the pending drafts of the user whose token reads it: a `@<username> [DRAFT] (draft:<id>):` block after a thread's published notes, and a `draft-<id>.txt` file headed `Discussion: (draft, not published)` for a draft that starts a thread. Drafts are nobody's activity on the MR: never report them as a note, a reply or a thread. **Refer to a thread by its full discussion id**, which is never ambiguous.

## Judgment rules

**Green is `success` and nothing else.** `canceled` is not green: nothing ran to completion and the job list shows no red, which is why it gets misread. `manual` is an unfinished blocking gate. `skipped` means no pipeline ran for this head. `running` and `pending` are not evidence yet. A pipeline counts only when its SHA equals the MR head; the scorecard says `on an older head` otherwise. A red pipeline on a branch that is behind its target ran on a superseded base, so judge it again after the rebase.

**Profile every AI reviewer on the MR at hand, assume nothing.** Reviewers do not behave alike, and the vocabulary one uses means nothing to another. Before reading any verdict, answer six questions per reviewer from its own threads: what it posts (inline findings, one summary, both); how it maintains the summary (edited in place, or re-posted per pass) and therefore where the latest verdict lives; whether it posts non-verdict notes (an acknowledgement, "review skipped, head unchanged" after a rebase) that you must walk past; whether it reads replies, which decides if an unresolved thread with a substantive reply is an active dialogue or a finding that clears only through a code change plus a manual resolve; the exact author string observed on this MR, since a service account can be an opaque hash and the `[BOT]` marker is not reliably set; and whether the verdict names the commit it reviewed, so you can date it against the head. Read the vocabulary the same way: the words a reviewer uses for "mergeable", "nothing actionable but a human must still review", and "actionable findings open" are its own, and the middle one is the one everyone misreads.

**If a memory already holds a reviewer's profile, use it and verify it once** against that reviewer's newest note. If the memory and the MR disagree, report the disagreement and derive the profile again. Once you derive a profile, **propose a Claude memory** for it, one per reviewer identity keyed by host plus author string, so the next run reads it instead of inferring it again. An illustrative block, with fictional values:

```
reviewer: @qa-review-bot   (host: gitlab.example.com, author.name: "QA Review Bot")
posts: inline findings + one summary comment
summary: edited in place — the last note of its General thread is the current verdict
non-verdict notes: "review skipped, head unchanged" after a rebase
reads replies: no — a finding clears by a code change plus a manual resolve
verdict names its head: yes — trailing "reviewed <short-sha>", compare it with the current head
verdict vocabulary: "Ready to merge" / "Needs work" / "No blocking findings"
```

**Read the latest verdict per reviewer, plus the count of currently unresolved threads. Never infer "this MR has problems" from historical finding notes.** A reviewer can post major findings on Monday, see them fixed on Tuesday, and post a clean summary on Wednesday; the old notes stay forever. A reviewer that edits its summary in place keeps the freshest verdict in one of the oldest thread files, so sort nothing by name. A verdict on a superseded head is stale evidence, not the current state.

**Separate human review from the author's own comments and from bots.** Take every author string from the threads and the MR's `author`, `assignees` and `reviewers` fields; never compute a slug. A human who only replies inside a bot's thread is often the most substantive reviewer on the MR, so count note authors across every thread, not only thread openers. An author's acceptance report is not review. A system note carrying a human's username ("changed this line in version N of the diff") is not a reply. Human approvals come from the approvals data, not from the threads, and an approval by an AI approver alone means the MR is still awaiting human approval.

**Then say what blocks the merge right now, and whose court the ball is in.** Red CI, draft, unresolved threads, conflicts, a missing approval, a blocking review. The author fixes, rebases or replies; the reviewer reviews and approves. State that every value is a snapshot: the target branch keeps moving.

## Reporting a set

When a caller hands over a list, write one Markdown file, one heading per MR with all of that MR's data under it, no overview table: `## <host>/<project>!<iid> — <short label>`, then `author`, `draft`, `rebased`, `mergeable`, `pipeline`, `approvals`, one line per AI reviewer, `human review`, `merge-gate`. Name every MR by its full identifier; an iid alone or an invented nickname is not an identifier. Keep it factual and promise no work.
