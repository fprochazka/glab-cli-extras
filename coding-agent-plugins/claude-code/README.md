# glab

Claude Code plugin for GitLab through the [glab CLI](https://gitlab.com/gitlab-org/cli) and [glab-cli-extras](https://github.com/fprochazka/glab-cli-extras). It ships in the same repository as the commands it teaches, so a skill always describes the commands of the release it ships with.

## Requirements

- `glab` installed, on `PATH`, and logged in to your GitLab host (`glab auth status`).
- glab-cli-extras of the same minor version as the plugin, which provides `glab-watch`, `glab-mr-status`, `glab-pipeline`, `glab-discussion` and the Bash guard:

  ```bash
  uv tool install glab-cli-extras
  ```

## Installation

```bash
uv tool install glab-cli-extras
claude plugin marketplace add fprochazka/glab-cli-extras --scope user
claude plugin install glab@fprochazka-glab-cli-extras --scope user
```

In a session: `/plugin marketplace add fprochazka/glab-cli-extras`, then `/plugin install glab@fprochazka-glab-cli-extras`.

### Replacing the older plugins

This plugin replaces three older ones: `glab` from the `fprochazka-claude-code-plugins` marketplace, `glab-pipeline` from `fprochazka-glab-pipeline` and `glab-discussion` from `fprochazka-glab-discussion`. Uninstall them, at every scope you installed them in, before you install this one:

```bash
claude plugin uninstall glab@fprochazka-claude-code-plugins --scope user
claude plugin uninstall glab-pipeline@fprochazka-glab-pipeline --scope user
claude plugin uninstall glab-discussion@fprochazka-glab-discussion --scope user
```

Two enabled plugins named `glab` collide: Claude Code loads the hooks of only one of them, and which skill wins is not defined. The old `glab-pipeline` and `glab-discussion` plugins carry Bash guards of their own, so with them still enabled a denied command comes back once per guard.

## Skills

- `glab:glab-pipeline`: `glab-pipeline inspect`, the dump it writes into the pipeline's dir, and the order to read it in: the summary first, then only the files it names.
- `glab:glab-discussion`: `glab-discussion` for reading threads into files and for writing, resolving, editing and deleting comments; the signature line on posted comments, the draft environment variable, and giving a review with a verdict.
- `glab:glab`: the glab CLI. Merge requests, issues, pipeline actions, releases, variables, schedules, labels, and the raw API with pagination and GraphQL, with a list of the commands the Bash guard denies and what to use instead.
- `glab:glab-watch`: starting `glab-watch` in the background so GitLab events arrive as messages, reading its header, filters and thresholds, reacting to each event kind, stopping a watcher, and the files it writes. `references/events.md` lists every event.
