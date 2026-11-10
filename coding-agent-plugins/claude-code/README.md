# glab

Claude Code plugin for GitLab through the [glab CLI](https://gitlab.com/gitlab-org/cli) and [glab-cli-extras](https://github.com/fprochazka/glab-cli-extras). It ships in the same repository as the commands it teaches, so a skill always describes the commands of the release it ships with.

## Requirements

- `glab` installed, on `PATH`, and logged in to your GitLab host (`glab auth status`).
- glab-cli-extras of the same minor version as the plugin, which provides `glab-watch`, `glab-mr-status`, `glab-pipeline`, `glab-discussion`, `glab-search-code` and the Bash guard:

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
- `glab:glab-search-code`: `glab-search-code` for a code search across GitLab projects, the files it downloads, and the order to read them in: `hits.json`, `metadata.json`, then `rg` over the files.
- `glab:glab`: the mistakes glab's own `--help` does not prevent: reading one page of a list and taking it for all of it, passing a long description or JSON body inline instead of through a file, and calling glab directly where a glab-cli-extras command does the job, with the forms the Bash guard denies.
- `glab:glab-watch`: starting `glab-watch` in the background so GitLab events arrive as messages, reading its header, filters and thresholds, reacting to each event kind, stopping a watcher, and the files it writes. `references/events.md` lists every event.

## Commands

Each command runs `glab-mr-status` for the MR of the current branch, as `glab` finds it, before Claude reads anything. `glab-mr-status --help` describes its output. None of them changes code.

- `/glab:overview`: the full MR state (pipeline, threads, external statuses) and what blocks the merge, without taking any action.
- `/glab:comments`: the threads, unresolved and resolved, with a proposal for each. It re-reads the resolved threads, because AI reviewers post findings that land there and a resolved thread does not prove the problem is fixed.
- `/glab:pipeline`: the pipeline dump, the failed jobs triaged and fixes proposed, reading the summary first and then only the files it points at.

## Hooks

- **PreToolUse, Bash**: `hooks/run-cc-hook.sh` runs `glab-cli-extras-cc-hook`, which denies inspecting pipelines and job logs with `glab ci view|get|trace` or the raw API, and reading or writing MR discussions with `glab api`, `glab mr view --comments` or `glab mr note`. The deny names the skill and the command to use instead. The wrapper always exits 0: when the command is missing or crashes, a Bash call goes through as if there were no guard, and the crash shows in `claude --debug`. So until glab-cli-extras is installed, the guard is inactive and allows every Bash call; the SessionStart check says so. This is deliberate: a guard that blocks Bash calls because of a missing install would stop the session's work. The bash guards of the old `glab-pipeline` and `glab-discussion` plugins fell back to text patterns when their classifier was missing; this guard has no such fallback.
- **SessionStart**: `hooks/check-glab-cli-extras.sh` prints nothing when all is well. Otherwise it asks the agent to tell you, with the command that fixes it, when glab-cli-extras is not installed, when it is older than the plugin or lacks a command the plugin uses, or when `glab-pipeline`, `glab-discussion` or `glab-search-code` on `PATH` still comes from the standalone packages that glab-cli-extras replaces.

## Permissions

The skills pre-approve the read-only commands they run in their `allowed-tools`: `glab-mr-status`, `glab-discussion read`, `glab-pipeline inspect`, `glab-search-code`, and the read-only `glab` subcommands and `--help` pages. The `/glab:comments` command carries `glab-discussion` in full, because it replies to and resolves threads, and the `/glab:pipeline` command carries `glab-pipeline` in full. Everything else, `glab-watch` included, asks the first time. To allow the skills to load without a prompt, add `Skill(glab:glab)` and the other skills by their full `glab:` names to `permissions.allow` in your settings.

## License

MIT
