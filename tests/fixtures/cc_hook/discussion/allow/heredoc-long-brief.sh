mkdir -p /tmp/work/mr-sweep/run-2 && cat > /tmp/work/mr-sweep/run-2/brief-collect.md <<'EOF'
# Brief - MR state re-collection, run 2

**First, invoke the `glab:mr-status` skill to load its usage guidance before running any
commands.** Also load `glab` and `glab-discussion`.

GitLab host is `gitlab.example.com`. Working dir for all outputs:
`/tmp/work/mr-sweep/run-2/`

This is a **re-collection**. The previous sweep's outputs are in `../run-1/`. Everything in
it is stale. **Do not trust any state from it** - it is there only so you can report what
changed.

## Step 1 - rediscover the open MR set

Repos (sweep **all** open MRs):
- `group/project` (`group%2Fproject`)
- `group/other-project` (`group%2Fother-project`)

Use `--paginate`. Write the full open-MR list to `open-mrs.tsv`, then `mr-set.tsv` with
columns `repo`, `iid`, `author`, `draft`, `collect_threads`.

## Step 2 - per-MR state

For every MR in `mr-set.tsv`, from
`glab api "projects/<enc>/merge_requests/<iid>?include_diverged_commits_count=true"`:

- `draft`, `state`, `title`, `web_url`, `sha`, `created_at`, `updated_at`
- `has_conflicts`, `merge_status`, `detailed_merge_status`, `diverged_commits_count`
- `head_pipeline.status`, `head_pipeline.sha`, `head_pipeline.id`
- approvals: `glab api "projects/<enc>/merge_requests/<iid>/approvals"` - who approved

**`has_conflicts` is uncomputed while `merge_status` is `checking`** - there `false` is a
default, not an answer. Report it as *unknown*, never as *no conflict*.

## Step 3 - threads, where `collect_threads` is `yes`

```bash
glab-discussion read --mr-url "https://gitlab.example.com/<repo>/-/merge_requests/<iid>" --dump
# Not these, they are blocked:
#   glab api "projects/<enc>/merge_requests/<iid>/discussions" --paginate
#   glab mr view <iid> --comments
```

Always `--mr-url`. **The grep token for an unresolved thread is `Resolved: no`, NOT
`Resolved: false`.**

## Hard rules

- **Read-only.** No rebase, no comment, no thread resolution, no approval.
- **Blocked by a wrapper - do not attempt:** `glab api` against an MR's `notes` or
  `discussions` sub-resource, `glab mr view --comments`, `glab mr note`, the pipeline-jobs API.
- For a red pipeline, name the failing job through the `glab-pipeline` skill.
- **Never measure an exit code through a pipe** - `cmd | head` reports `head`'s status.
  Redirect to a file, then inspect.
- `glab api` output can carry control characters that break `jq`. Redirect to a file first.
- **Every list endpoint paginates at 20.** Always `--paginate` where a count matters.

## Output

Write `mr-review-state.md` in the run dir: a summary table with one row per MR, then one
section per MR headed `## <repo>!<iid> - <MR title>`.

Also write `changes-vs-run-1.md`: MRs that are **new** since `../run-1/mr-set.tsv`, MRs
from that set that are now **merged or closed**, and MRs whose draft flag changed.

Report back: counts collected, any MR that failed to collect and why.
EOF
echo written; wc -l /tmp/work/mr-sweep/run-2/brief-collect.md
