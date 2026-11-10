---
name: glab:glab-discussion
description: >-
  This skill should be used when the user asks to "read MR discussions", "list MR comments",
  "reply to a discussion", "reply to a review comment", "resolve a discussion", "edit a comment",
  "delete a comment", "add a diff note", "review MR comments", "dump discussions",
  "show MR diff for commenting", "review an MR", "write review comments",
  "post review comments as drafts", "publish drafts", "submit a review", "sign the comment",
  or needs to interact with GitLab merge request discussions or draft notes. Provides the
  reference for the glab-discussion CLI.
---

# glab-discussion

`glab-discussion` reads a merge request's discussion threads into one file per thread, and writes comments, replies, resolves, edits, deletes and reviews. It comes with glab-cli-extras (`uv tool install glab-cli-extras`).

Use it instead of `glab mr note`, `glab mr view --comments` and raw `glab api …/notes`, `…/discussions` or `…/draft_notes` calls. The plugin's Bash guard denies those. Award emoji stay with `glab api`.

**The MR is found from the current branch** through `glab mr view`, so no flag is needed when the source branch is checked out. Otherwise pass `--mr-url <url>`, or all of `--hostname`, `--project` and `--mr-iid`. A partial set is an error, never a silent fallback to the current branch's MR.

## read

```bash
glab-discussion read                    # dump mode when not in a terminal: one file per thread, then what changed
glab-discussion read --mr-url <url>     # an explicit MR
glab-discussion read --full             # delete the thread files and write them all again
glab-discussion read --no-dump          # print every thread instead
```

`read` writes one `.txt` per thread into the `threads/` dir of the MR's dir and prints the dir and the files that changed:

```text
Discussions: /tmp/glab-cli-extras-1000/gitlab.example.com/group/project/-/merge_requests/123/threads/
  updated: 2026-11-09T09-30-reviewer-one-6a1f2b3c4d5e.txt
  (11 discussions up to date)
```

Take the dir from that line, never build it. A rerun fetches only the threads whose notes changed, so **read only the files it lists**. `glab-watch mr` writes the same files, and its `thread_*` events carry the file's path. `glab-mr-status` runs a `read` too.

### The thread file

- A header: `Discussion: <full id>`, `Type: General` or `DiffNote`, `File:` and `Line:` for a diff note, `Resolved: yes` or `no` only on a resolvable thread, and the URL.
- Then one `[<created>] @<username> (note:<id>):` block per note, in order. A bot author gets a `[BOT]` tag and a `bot-` file name prefix; a service account shows its display name.
- The file shows creation times only, so an edited note keeps its old time. The `updated:` listing is what tells you it changed.
- Your own pending drafts: a `@<you> [DRAFT] (draft:<id>):` block after a thread's published notes (`[DRAFT, resolves thread]` when publishing it resolves the thread), and a `draft-<id>.txt` file headed `Discussion: (draft, not published)` for a draft that starts a thread. Drafts are nobody's activity on the MR: never report them as a note or a reply.

### Ids

- **A discussion**: the full id from the `Discussion:` line. `--reply-to` and `resolve` also take the 12-character prefix from a file name, but the full id is never ambiguous.
- **A note or a draft**: `note:<id>` or `draft:<id>`, exactly as `read` prints them. A bare number is rejected, because a draft and a note can have the same number.
- **A GitLab UI link** to a note ends in `#note_<id>`. Find its thread with `grep -rl "note:<id>" <threads dir>`; a draft never appears in a URL.

## diff

```bash
glab-discussion diff                     # every changed file, with old and new line numbers
glab-discussion diff --file src/app.py   # one file
glab-discussion diff --version <id>      # an older diff version
```

The output shows `old | new |` columns for each line; use them for `--old-line` and `--new-line`.

## write

Pass every body from a file with `--body - < file`. When the file is missing, the shell fails before the command runs, while `--body "$(cat file)"` posts an empty comment, which `write` then refuses.

```bash
glab-discussion write --body - < comment.md                                # a new general thread
glab-discussion write --reply-to <discussion id> --body - < reply.md       # a reply
glab-discussion write --file src/app.py --new-line 42 --body - < note.md   # a diff note on the newest diff version
glab-discussion write --file src/app.py --old-line 10 --body - < note.md   # on a removed line
glab-discussion write --file src/app.py --new-line 42 --commit <sha> --body - < note.md
```

- `--new-line` is the line in the MR's source branch, so a local checkout of it gives the number directly. `--new-line` alone works on an unchanged context line: the command adds the old line number GitLab needs there. A line outside every hunk is refused, since GitLab takes diff notes only on changed lines and their context.
- `--draft` writes a pending draft instead (it prints `draft:<id>`; a published comment prints `note:<id>`). With `--reply-to --draft`, `--resolve` resolves the thread when the draft is published.
- **One draft reply per thread.** GitLab refuses a second draft reply in a thread where you already have one, and the error names that `draft:<id>`. Put the new text into it with `edit draft:<id>`, or delete it and write again.
- **`GLAB_DISCUSSION_WRITE_AS_DRAFT=true` makes every `write` a draft** unless `--no-draft` is given; the output says so on a second line. When it says so, the comment is not posted yet. Publish it, or tell the user that it waits as a draft.

### Signing a posted comment

Every comment a Claude Code command posts on an MR (a thread, a reply, a summary, a diff note) ends with a blank line and an HTML comment naming the command that caused it:

```
<!-- <plugin>:<command> -->
```

The line is invisible in rendered Markdown and stays in the API body: the write commands send the body byte for byte. It tells a reader which automation wrote the comment, and it lets a later pass of the same command recognize its own threads. Recognize a signature by **last-line equality**, never by a substring match: review prose often quotes a marker in backticks. A command that runs another command's posting steps signs with its own name, since it is the cause.

## resolve, edit, delete

```bash
glab-discussion resolve <discussion id>               # --unresolve to reopen
glab-discussion edit note:<id> --body - < new.md      # or draft:<id>
glab-discussion delete note:<id>                      # or draft:<id>
```

`resolve` reads the thread first and refuses one without a `Resolved:` line in its file, because GitLab cannot resolve it. A reply or resolve that gets 404 says whether the thread is gone or the error was transient. Each command prints the URL of the MR it acted on.

## Giving a review

Use drafts only for a review with many comments on one MR. Every published comment notifies everyone who watches the MR, once per comment; drafts arrive as one review with one notification. Single replies and quick answers stay normal comments.

1. Write each comment with `write --draft` (inline with `--file` and `--new-line`, replies with `--reply-to`).
2. Check the drafts with `glab-discussion read`; fix them with `edit draft:<id>` or `delete draft:<id>`.
3. Publish with `glab-discussion drafts publish --body - < summary.md --verdict <verdict>`.

```bash
glab-discussion drafts publish --verdict reviewed            # reviewed | requested-changes | approve
glab-discussion drafts delete                                # list your drafts, delete nothing
glab-discussion drafts delete --force                        # delete them all
```

- **Every review carries a verdict.** Without `--verdict` it is `reviewed`, but only when you have given the MR no verdict yet. If you have, `drafts publish` publishes nothing and exits non-zero: decide which verdict this review gives, ask the user when unsure, and pass `--verdict`.
- **`drafts publish` can stop with `Error: … Nothing was published.`** for two more reasons. The drafts stay pending, and the summary is not added yet.
  - **You are not a reviewer.** GitLab sets a verdict only for reviewers, so the command adds you, keeping the other reviewers, and checks that GitLab did; this adds a system note. GitLab drops a reviewer it cannot add without an error, for example on an MR that allows one reviewer when someone else is it. Tell the user: making you the reviewer means replacing another one, and that is their call.
  - **Your earlier verdict could not be removed.** GitLab before 19.2 does not replace a verdict, so the command first revokes your approval when the new verdict is not `approve`, and removes your request for changes when it is not `requested-changes` (GitLab EE 17.8+). Tell the user what GitLab answered. Do not pick another verdict just to get past the error.
- Afterwards it reads the verdict back and exits non-zero when it was not applied. Tell the user what it reports.
- Drafts belong to the GitLab login that wrote them. When glab runs as the user, the user sees them in the GitLab review panel too.

## Resolved threads can hide findings

A resolved thread does not prove the problem is fixed. AI reviewers post findings that land resolved, and a thread can be resolved by its author without a change. When asked what is left to do on an MR, read the resolved threads as well as the unresolved ones and check each finding against the code.
