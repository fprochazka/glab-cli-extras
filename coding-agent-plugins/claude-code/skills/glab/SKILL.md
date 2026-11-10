---
name: glab:glab
description: >-
  This skill should be used before running the glab CLI for anything that lists, sends a body, or
  reads MR, pipeline or review state: "call the GitLab API", "glab api", "list all merge requests",
  "list every issue", "fetch all pages", "update the MR description", "create an MR with a
  description", "post JSON to the API", "run a GraphQL query". Covers pagination that silently
  returns one page, passing long bodies through files instead of inline, and which glab-cli-extras
  command replaces a raw glab call (glab-pipeline, glab-discussion, glab-watch, glab-mr-status,
  glab-search-code).
trigger-keywords: gitlab, glab, glab api, merge request, MR, paginate
allowed-tools: Bash(glab --help), Bash(glab help:*), Bash(glab alias --help), Bash(glab api --help), Bash(glab auth --help), Bash(glab changelog --help), Bash(glab ci --help), Bash(glab ci artifact --help), Bash(glab ci cancel --help), Bash(glab ci config --help), Bash(glab ci delete --help), Bash(glab ci lint --help), Bash(glab ci list --help), Bash(glab ci retry --help), Bash(glab ci run --help), Bash(glab ci run-trig --help), Bash(glab ci status --help), Bash(glab ci trigger --help), Bash(glab cluster --help), Bash(glab config --help), Bash(glab deploy-key --help), Bash(glab duo --help), Bash(glab gpg-key --help), Bash(glab incident --help), Bash(glab issue --help), Bash(glab issue board --help), Bash(glab issue close --help), Bash(glab issue create --help), Bash(glab issue delete --help), Bash(glab issue list --help), Bash(glab issue note --help), Bash(glab issue reopen --help), Bash(glab issue subscribe --help), Bash(glab issue unsubscribe --help), Bash(glab issue update --help), Bash(glab issue view --help), Bash(glab iteration --help), Bash(glab iteration list --help), Bash(glab job --help), Bash(glab label --help), Bash(glab mcp --help), Bash(glab mcp serve --help), Bash(glab milestone --help), Bash(glab mr --help), Bash(glab mr approve --help), Bash(glab mr approvers --help), Bash(glab mr checkout --help), Bash(glab mr close --help), Bash(glab mr create --help), Bash(glab mr delete --help), Bash(glab mr diff --help), Bash(glab mr for --help), Bash(glab mr issues --help), Bash(glab mr list --help), Bash(glab mr merge --help), Bash(glab mr rebase --help), Bash(glab mr reopen --help), Bash(glab mr revoke --help), Bash(glab mr subscribe --help), Bash(glab mr todo --help), Bash(glab mr unsubscribe --help), Bash(glab mr update --help), Bash(glab mr view --help), Bash(glab opentofu --help), Bash(glab release --help), Bash(glab repo --help), Bash(glab schedule --help), Bash(glab securefile --help), Bash(glab snippet --help), Bash(glab ssh-key --help), Bash(glab stack --help), Bash(glab token --help), Bash(glab user --help), Bash(glab variable --help), Bash(glab issue list:*), Bash(glab issue view:*), Bash(glab ci artifact:*), Bash(glab ci lint:*), Bash(glab ci list:*), Bash(glab ci status:*), Bash(glab job artifact:*), Bash(glab mr approvers:*), Bash(glab mr diff:*), Bash(glab mr issues:*), Bash(glab mr list:*), Bash(glab mr view:*), Bash(glab schedule list:*)
---

# glab

The glab CLI documents itself: run `glab <command> --help` before the first use of a command. This skill covers only the three mistakes that `--help` does not prevent: reading one page and taking it for the whole list, putting a long body inline in the command, and doing by hand what a glab-cli-extras command does better.

## Pagination

A list endpoint returns one page, and nothing in the output says that more pages exist. A count, a "not found in the list", or a "this is everything" taken from one page is wrong without any error.

- **`glab api` returns 20 items** (GitLab's default) unless you pass `--paginate`. With `--paginate`, glab asks for `per_page=100` (unless you set `per_page` yourself) and fetches every page.
- **The output of `--paginate` is one JSON array** in the default `--output json`: glab merges the pages, so `jq` reads it as is, without `-s`. `--output ndjson` prints one element per line, which suits `jq 'select(…)'` on a long list.

  ```bash
  glab api "projects/:id/merge_requests?state=opened" --paginate | jq 'length'
  glab api projects/:id/issues --paginate --output ndjson | jq -c 'select(.labels | index("bug"))'
  ```

- **To check one page**, add `-i` and read the `x-next-page` and `x-total-pages` headers. An empty `x-next-page` means the last page. GitLab leaves out the total headers on very large collections, so an absent `x-total` does not mean "everything".
- **GraphQL pages by cursor.** `--paginate` works only when the query takes `$endCursor: String` and selects `pageInfo { hasNextPage endCursor }` on the collection. Without both, you get the first page and no warning.
- **The list commands page too.** `glab mr list` and `glab issue list` show 30 items, `glab ci list` 20. Raise it with `-P <n>` (at most 100) or walk `--page`. In `glab mr list`, `--all` means every state, open, merged and closed, not every page.

## Long bodies go into a file

Write a description, a comment, a JSON body or a GraphQL query into a file first, then pass the file. Inline text breaks on shell quoting, hides the content inside a long command, and has to be rewritten whole for every correction. Put the file in the session scratchpad, the dir Claude Code names for temporary files, not in `/tmp`. Name it after what it belongs to (`mr-123-description.md`), and reuse it for the next edit.

- **MR and issue descriptions**: `--description-file <file>` on `glab mr create`, `glab mr update` and `glab issue create`. Never `--description -`, which opens an editor and waits.
- **Release notes**: `glab release create <tag> --notes-file <file>`.
- **A JSON body for `glab api`**: `--input <file>`. glab sends the file as is, as `application/json`. Any `-F` or `-f` given with `--input` becomes a query parameter, not part of the body.

  ```bash
  glab api -X PUT projects/:id/merge_requests/123 --input <scratchpad>/mr-123-update.json
  ```

- **One field from a file**: `-F <name>=@<file>` puts the file's content into that field. This also works for a GraphQL query: `glab api graphql -F query=@<scratchpad>/open-mrs.graphql`.
- **An option with no file variant**: pass the file's content with a quoted command substitution, `--<option> "$(cat <scratchpad>/<file>)"`. The double quotes keep the whole file as one argument, and the shell does not expand anything inside the substituted text. Prefer a `-file` option or `@file` where one exists. MR comments and replies are not a case for this: they go through `glab-discussion`, below.

`-F` (`--field`) and `-f` (`--raw-field`) are not the same flag with two spellings:

- `-F` converts the value. `@` at the start reads a file, so `-F body='@alice please look'` fails because there is no file named `alice please look`. `true`, `false`, `null` and integers become JSON types, so a title `-F title=2026` is sent as a number. A value that starts with `[` or `{` is parsed as JSON.
- `-f` sends the value as a string, unchanged. Use it for any text that a person wrote.

When you must write the file from the shell, quote the heredoc delimiter (`<<'EOF'`), so the shell does not expand `$`, backticks or `\` in the body. Pick a delimiter that cannot occur as a line of the body. Inside double quotes, Markdown backticks in a value run as commands, which is one more reason to write the file instead.

## Use the glab-cli-extras commands for their jobs

glab-cli-extras ships commands that do these jobs in one call and write the result to files. The plugin's Bash guard denies the raw forms marked below and names the command and the skill to use.

- **Pipelines, jobs and job logs**: `glab-pipeline inspect`, see the `glab:glab-pipeline` skill. Denied: `glab ci view`, `glab ci get`, `glab ci trace`, and `glab api` on `…/pipelines/<id>…`, `…/jobs/<id>/trace` or `…/ci/lint`. Allowed: the pipeline list endpoint (`…/pipelines?ref=…`) and the actions `glab ci run`, `glab ci retry`, `glab ci trigger`, `glab ci cancel`.
- **MR threads, comments, reviews and review verdicts**: `glab-discussion`, see the `glab:glab-discussion` skill. It reads threads into files, writes, resolves, edits and deletes comments, and publishes a review with a verdict (`drafts publish --verdict approve`). Denied: `glab mr note`, `glab mr view --comments`, and `glab api` on an MR's `notes`, `discussions` or `draft_notes`. Allowed: award emoji through `glab api`, which glab-discussion has no command for.
- **Waiting for a change**: `glab-watch` in the background, see the `glab:glab-watch` skill. It tells the session when a pipeline finishes or someone comments. The guard does not stop a `sleep` loop, but never poll: one watcher costs a fraction of the requests.
- **The current state of an MR**: `glab-mr-status`, one read of the pipeline, approvals, threads and what changed since the last read. `glab-mr-status --help` explains its output.
- **Code search across projects**: `glab-search-code '<query>'`, see the `glab:glab-search-code` skill. It downloads every matching file, so you grep files instead of paging through `glab api "search?scope=blobs…"`. One raw page of a blob search is fine for a quick look, but not for "nothing else uses it".
