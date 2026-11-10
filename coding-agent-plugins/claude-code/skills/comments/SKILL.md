---
name: glab:comments
description: Fix MR comment issues (unresolved and missed comments)
allowed-tools: ["Bash(glab-mr-status:*)", "Bash(glab-discussion:*)"]
---

## Context

```!
glab-mr-status --comments
```

## Your task

`glab-mr-status` dumped the discussions into per-thread files in the threads directory listed above.
Read the discussion files to understand each thread, then analyze and propose what to do about them. No implementation yet.
Load the `glab:glab-discussion` skill before you write anything.

### 1. Unresolved Comments
Review all unresolved discussion threads:
- For each unresolved comment, understand what is being requested
- Propose what to do about each one (fix code, respond, or explain why no action needed)

### 2. Resolved Comments Verification
Review resolved comments as well because:
- Some problems marked as resolved might not actually be fixed
- AI reviewers and other automated tools sometimes post comments that appear in "resolved" instead of "unresolved"
- Look for any actionable feedback that may have been missed

### 3. Interacting with Discussions

Each discussion file includes the `Discussion:` ID in its header. Use `glab-discussion` to interact, with every body in a file:

**Reply to a discussion:**
```bash
glab-discussion write --reply-to <discussion_id> --body - < reply.md
```

**Resolve a discussion:**
```bash
glab-discussion resolve <discussion_id>
```

**Add a diff note on a specific line:**
```bash
glab-discussion write --file <path> --new-line <n> --body - < note.md
```

**Sign every comment you post.** End the body with a blank line and `<!-- glab:comments -->`, so a later pass recognizes its own comments by the last line.

**Drafts.** When `GLAB_DISCUSSION_WRITE_AS_DRAFT=true` is set, `write` saves a draft instead of posting, and its output says so on a second line. Then the reply is not visible to anyone yet: tell the user, and publish the drafts with `glab-discussion drafts publish` only when the user asks.

## Additional instructions

$ARGUMENTS
