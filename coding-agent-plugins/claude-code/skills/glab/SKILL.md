---
name: glab
description: >-
  This skill should be used when the user works with GitLab through the glab CLI: "create an MR",
  "update the MR description", "mark the MR ready", "merge the MR", "list merge requests", "list issues",
  "call the GitLab API", "glab api", CI variables, releases, schedules, labels. For MR comments use
  glab-discussion, for a failed pipeline glab-pipeline, for waiting on CI or review and for review state
  glab-watch.
trigger-keywords: gitlab, glab, merge request, MR
allowed-tools: Bash(glab --help), Bash(glab help:*), Bash(glab alias --help), Bash(glab api --help), Bash(glab auth --help), Bash(glab changelog --help), Bash(glab ci --help), Bash(glab ci artifact --help), Bash(glab ci cancel --help), Bash(glab ci config --help), Bash(glab ci delete --help), Bash(glab ci lint --help), Bash(glab ci list --help), Bash(glab ci retry --help), Bash(glab ci run --help), Bash(glab ci run-trig --help), Bash(glab ci status --help), Bash(glab ci trigger --help), Bash(glab cluster --help), Bash(glab config --help), Bash(glab deploy-key --help), Bash(glab duo --help), Bash(glab gpg-key --help), Bash(glab incident --help), Bash(glab issue --help), Bash(glab issue board --help), Bash(glab issue close --help), Bash(glab issue create --help), Bash(glab issue delete --help), Bash(glab issue list --help), Bash(glab issue note --help), Bash(glab issue reopen --help), Bash(glab issue subscribe --help), Bash(glab issue unsubscribe --help), Bash(glab issue update --help), Bash(glab issue view --help), Bash(glab iteration --help), Bash(glab iteration list --help), Bash(glab job --help), Bash(glab label --help), Bash(glab mcp --help), Bash(glab mcp serve --help), Bash(glab milestone --help), Bash(glab mr --help), Bash(glab mr approve --help), Bash(glab mr approvers --help), Bash(glab mr checkout --help), Bash(glab mr close --help), Bash(glab mr create --help), Bash(glab mr delete --help), Bash(glab mr diff --help), Bash(glab mr for --help), Bash(glab mr issues --help), Bash(glab mr list --help), Bash(glab mr merge --help), Bash(glab mr rebase --help), Bash(glab mr reopen --help), Bash(glab mr revoke --help), Bash(glab mr subscribe --help), Bash(glab mr todo --help), Bash(glab mr unsubscribe --help), Bash(glab mr update --help), Bash(glab mr view --help), Bash(glab opentofu --help), Bash(glab release --help), Bash(glab repo --help), Bash(glab schedule --help), Bash(glab securefile --help), Bash(glab snippet --help), Bash(glab ssh-key --help), Bash(glab stack --help), Bash(glab token --help), Bash(glab user --help), Bash(glab variable --help), Bash(glab issue list:*), Bash(glab issue view:*), Bash(glab ci artifact:*), Bash(glab ci lint:*), Bash(glab ci list:*), Bash(glab ci status:*), Bash(glab job artifact:*), Bash(glab mr approvers:*), Bash(glab mr diff:*), Bash(glab mr issues:*), Bash(glab mr list:*), Bash(glab mr view:*), Bash(glab schedule list:*)
---

# glab

Command-line interface for GitLab. Binary name: `glab`. glab-cli-extras adds commands for what glab does poorly for an agent; the "Use instead" section lists them.

## Repository context

glab finds the GitLab project from the git remote of the current directory. Override it with `--repo` (`-R`):

```bash
glab mr list                              # the project of the current directory
glab mr list -R group/project             # an explicit project
glab mr list -R group/namespace/project
```

Outside a checkout, glab falls back to its default host, which can be the wrong one. When the MR or project is not the current directory's, name it by URL or with `-R` and `--hostname`.

## Identifying MRs

MR commands take an MR IID, a branch name, or nothing (the current branch):

```bash
glab mr view 123           # by MR IID
glab mr view feature-x     # by branch name
glab mr view               # the current branch's MR
```

## Files for long arguments

For any argument longer than a few words (an MR description, a comment), put the content in a file and pass it with `"$(cat <path>)"`, or `--body - < <path>` where the command reads stdin. Edits stay cheap, and the user sees the diff of the file.

- If the content already exists as a file, use it directly; do not copy it first.
- Name a new file after the MR, branch or job it belongs to (`/tmp/mr-desc-123.md`), never a generic name that collides across MRs. Reuse the same file for the same MR.

## Creating a merge request

```bash
glab mr create --fill --fill-commit-body --yes -b main --draft -a user1 --reviewer user2 -l bug
```

| Flag | Short | Description |
|------|-------|-------------|
| `--title` | `-t` | MR title |
| `--description` | `-d` | Description (`"-"` opens an editor) |
| `--fill` | `-f` | Use the commit info, push the branch |
| `--fill-commit-body` | | Include all commit bodies (with `--fill`) |
| `--draft` | | Create as draft |
| `--assignee` | `-a` | Assign by username (comma-separated or repeated) |
| `--reviewer` | | Request review by username |
| `--label` | `-l` | Add labels |
| `--milestone` | `-m` | Assign a milestone |
| `--target-branch` | `-b` | Target branch |
| `--source-branch` | `-s` | Source branch (default: current) |
| `--push` | | Push the branch before creating |
| `--remove-source-branch` | | Remove the source branch on merge |
| `--squash-before-merge` | | Squash commits on merge |
| `--related-issue` | `-i` | Link to an issue IID |
| `--yes` | `-y` | Skip the confirmation prompt |

## Viewing a merge request

```bash
glab mr view -F json
```

For the review state (pipeline, approvals, threads, what changed since you last looked), run `glab-mr-status` instead; see the glab-watch skill. For the threads, use `glab-discussion read`, never `glab mr view --comments`.

## Updating title, description and state

```bash
glab mr update 123 --title "New title" --description "$(cat /tmp/mr-desc-123.md)" --ready -l bug --reviewer +user2
```

Assignee and reviewer prefixes: `+` adds, `!` or `-` removes, no prefix replaces all.

| Flag | Short | Description |
|------|-------|-------------|
| `--title` | `-t` | MR title |
| `--description` | `-d` | Description (`"-"` opens an editor) |
| `--draft` | | Mark as draft |
| `--ready` | `-r` | Mark as ready |
| `--label` | `-l` | Add labels |
| `--unlabel` | `-u` | Remove labels |
| `--assignee` | `-a` | Set assignees (`+` add, `!` remove, bare replaces all) |
| `--reviewer` | | Set reviewers (`+` add, `!` remove, bare replaces all) |
| `--milestone` | `-m` | Set a milestone (`""` or `0` to unset) |
| `--target-branch` | | Change the target branch |
| `--yes` | `-y` | Skip the confirmation |

`glab mr merge`, `glab mr approve`, `glab mr revoke`, `glab mr rebase` and `glab mr close` act on the MR; check their `--help` before the first use.

## Pipelines: actions only

```bash
glab ci status --compact           # the current branch's pipeline, in short
glab ci list                       # recent pipelines
glab ci run                        # start a pipeline on the current branch
glab ci retry <job-name>           # retry a failed job
glab ci trigger <job-name>         # start a manual job
glab ci cancel pipeline <id>       # cancel a running pipeline; `glab ci cancel job <id>` for one job
glab ci lint .gitlab-ci.yml        # lint a local CI file
```

To find out why a pipeline failed, run `glab-pipeline inspect`; see the glab-pipeline skill. To wait for one, run `glab-watch pipeline` in the background; see the glab-watch skill.

## API (raw HTTP requests)

```bash
glab api projects/:id/members                                    # GET (the default)
glab api projects/:id/labels -f name=bug -f color=#FF0000        # POST (the default with parameters)
glab api -X PUT projects/:id -f name="new-name"                  # an explicit method
glab api graphql -f query='query { currentUser { username } }'   # GraphQL
```

### Pagination

`--paginate` fetches every page. Without it, you get the first page only, 20 items by default.

```bash
glab api projects/:id/issues --paginate --output ndjson | jq 'select(.state == "opened")'
```

Output formats: `json` (the default, pages merged into one array) and `ndjson` (one object per line, for `jq`).

### Endpoint placeholders

Resolved from the git remote: `:id`, `:fullpath`, `:group`, `:namespace`, `:repo`, `:branch`, `:user`, `:username`.

### Parameter flags

| Flag | Short | Description |
|------|-------|-------------|
| `--raw-field` | `-f` | String parameter |
| `--field` | `-F` | Typed parameter (`true`/`false`/integers converted, `@file` reads a file) |
| `--method` | `-X` | HTTP method (default: GET without parameters, POST with) |
| `--paginate` | | Fetch every page |
| `--output` | | `json` (default) or `ndjson` |
| `--header` | `-H` | An extra HTTP header |
| `--hostname` | | The GitLab host |
| `--include` | `-i` | Print the response headers |
| `--input` | | The request body from a file (`-` for stdin) |
| `--silent` | | Print no response body |

## Use instead

The plugin's Bash guard denies these, and names the replacement when it does:

- `glab mr note`, `glab mr view --comments`, and `glab api` on an MR's `notes`, `discussions` or `draft_notes`: use `glab-discussion` (read, write, resolve, edit, delete, drafts). Award emoji stay with `glab api`.
- `glab ci view`, `glab ci get`, `glab ci trace`, and `glab api` on `…/pipelines/<id>…`, `…/jobs/<id>/trace` or `…/ci/lint`: use `glab-pipeline inspect`. The pipelines list endpoint (`…/pipelines?ref=…`) stays allowed.
- A loop or `sleep` that polls an MR or a pipeline: run `glab-watch` in the background, which tells the session when something changes.

## Other commands

Discover their flags with `--help`:

| Group | Description |
|-------|-------------|
| `mr list`, `mr diff`, `mr merge`, `mr approve`, `mr rebase`, `mr checkout` | Merge requests |
| `issue list`, `issue view`, `issue create`, `issue update`, `issue close` | Issues |
| `ci list`, `ci run`, `ci lint`, `ci cancel` | Pipelines |
| `job artifact` | Download job artifacts |
| `repo view`, `repo list`, `repo clone` | Repositories |
| `release list`, `release view`, `release create` | Releases |
| `variable list`, `variable get`, `variable set` | CI/CD variables |
| `schedule list`, `schedule create`, `schedule run` | Pipeline schedules |
| `label list`, `label create` | Labels |
| `milestone list`, `milestone create` | Milestones |

```bash
glab <command> --help
glab <command> <subcommand> --help
```
