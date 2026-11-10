# Judging an MR's review state

These rules apply to what `glab-mr-status` prints and to what a `glab-watch mr` event reports. Neither command can make these calls for you.

## The pipeline

**Green is `success` and nothing else.**

- `canceled` is not green. Nothing ran to completion and the job list shows no red, which is why it gets misread.
- `manual` is an unfinished blocking gate.
- `skipped` means no pipeline ran for this head.
- `running` and `pending` are not evidence yet.

A pipeline counts only when its SHA equals the MR head; the `glab-mr-status` scorecard says `on an older head` otherwise. A red pipeline on a branch that is behind its target ran on a superseded base, so judge it again after the rebase.

## AI reviewers

**Profile every AI reviewer on the MR at hand, and assume nothing.** Reviewers do not behave alike, and the words one uses mean nothing to another. Before you read any verdict, answer these questions per reviewer from its own threads:

1. What does it post: inline findings, one summary, or both?
2. Does it edit its summary in place or post a new one per pass? That decides where the latest verdict lives.
3. Does it post notes that are not verdicts, such as an acknowledgement or "review skipped, head unchanged" after a rebase? Walk past those.
4. Does it read replies? If yes, an unresolved thread with a substantive reply is an active dialogue. If no, a finding clears only through a code change plus a manual resolve.
5. What is its exact author string on this MR? A service account can be an opaque hash, and the `[BOT]` marker is not reliably set.
6. Does its verdict name the commit it reviewed? Then you can date the verdict against the head.

Read its vocabulary the same way. The words a reviewer uses for "mergeable", "nothing actionable, but a human must still review" and "actionable findings open" are its own. The middle one is the one everyone misreads.

**If a memory already holds a reviewer's profile, use it and check it once** against that reviewer's newest note. If the memory and the MR disagree, report the disagreement and derive the profile again. Once you derive a profile, **propose a Claude memory** for it, one per reviewer identity keyed by host plus author string, so the next run reads it instead of inferring it again. An example, with fictional values:

```text
reviewer: @qa-review-bot   (host: gitlab.example.com, author.name: "QA Review Bot")
posts: inline findings + one summary comment
summary: edited in place — the last note of its General thread is the current verdict
non-verdict notes: "review skipped, head unchanged" after a rebase
reads replies: no — a finding clears by a code change plus a manual resolve
verdict names its head: yes — trailing "reviewed <short-sha>", compare it with the current head
verdict vocabulary: "Ready to merge" / "Needs work" / "No blocking findings"
```

**Read the latest verdict per reviewer, plus the count of threads unresolved now.** Never infer "this MR has problems" from old finding notes. A reviewer can post major findings on Monday, see them fixed on Tuesday, and post a clean summary on Wednesday; the old notes stay forever. A reviewer that edits its summary in place keeps the newest verdict in one of the oldest thread files, so do not sort by file name. A verdict on a superseded head is stale evidence, not the current state.

## Humans, the author and bots

Keep human review apart from the author's own comments and from bots.

- Take every author string from the threads and from the MR's `author`, `assignees` and `reviewers` fields. Never compute a username from a display name.
- Count note authors across every thread, not only the people who opened threads. A human who only replies inside a bot's thread is often the most substantive reviewer on the MR.
- The author's report that a finding is accepted or fixed is not review.
- A system note that carries a human's username, such as "changed this line in version N of the diff", is not a reply.
- Human approvals come from the approvals data, not from the threads. An approval by an AI approver alone means the MR still waits for a human approval.
- Your own signed comments are yours: the glab-discussion skill says how to recognize the signature.

## What blocks the merge

Say what blocks the merge right now, and whose turn it is: red CI, draft, unresolved threads, conflicts, a missing approval, a blocking review. The author fixes, rebases or replies; the reviewer reviews and approves. Every value is a snapshot, because the target branch keeps moving.
