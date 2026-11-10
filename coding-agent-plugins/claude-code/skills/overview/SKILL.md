---
name: glab:overview
description: Show MR overview (pipeline status, comments, external statuses)
allowed-tools: ["Bash(glab-mr-status:*)", "Bash(glab-pipeline inspect:*)", "Bash(glab-discussion read:*)"]
---

## Context

```!
glab-mr-status --all
```

## Your task

Report the state of the merge request. Take no action and change no code.

Cover the pipeline, the discussion threads, the external commit statuses, and what blocks the merge right now.
`glab-mr-status --help` explains the change lines and the files above.

### Reading the pipeline dump

`glab-mr-status` already ran `glab-pipeline inspect` for you. Read the summary text file it named first: it names each failed job, its stage, and its failure reason, and it points at the file that explains it.
Then open only the files the summary names: `job-logs/<failed-job>.log` for a script failure, `test-report.json` for test failures, `lint.json` plus `merged.yml` for a config error, `downstream/<bridge>-<id>.json` for a failed child pipeline.
Do not read the logs of jobs that passed. A green pipeline needs no log at all.

`summary.json` in the same directory holds the same summary as structured data, for `jq` queries.

Invoke the `glab:glab-pipeline` skill before you run the CLI directly, for example to inspect again after a new push.

### Reading the discussions

`glab-mr-status` wrote one file per thread into the threads directory listed above. Read those files. Do not fetch the discussions again.

When the output lists changes since the last read, lead the overview with them. When the command says `glab-mr-status: command not found`, tell the user to install it with `uv tool install glab-cli-extras`, and ask before installing.

## Additional instructions

$ARGUMENTS
