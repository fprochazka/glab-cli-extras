---
name: glab-pipeline
description: >-
  This skill should be used when the user asks to "inspect a pipeline", "why did the pipeline fail",
  "debug a failed CI job", "look at the job log", "check the MR pipeline", "dump the pipeline state",
  "investigate a CI failure", or when a glab-watch event reports a failed pipeline or job. Covers
  `glab-pipeline inspect`, the files it writes, and the order to read them in.
---

# glab-pipeline

`glab-pipeline inspect` dumps a GitLab CI pipeline into files (the pipeline, every job, the logs of the failed jobs, and the lint, test report and downstream details when they apply) and prints a problem-driven summary. It comes with glab-cli-extras (`uv tool install glab-cli-extras`).

Use it instead of `glab ci view`, `glab ci get`, `glab ci trace` and raw `glab api …/pipelines/…`, `…/jobs/…/trace` or `…/ci/lint` calls. The plugin's Bash guard denies those. Actions stay with `glab ci` (see the end).

## Running it

The pipeline is found from the current branch's MR through `glab mr view`, so no flag is needed when the source branch is checked out.

```bash
glab-pipeline inspect                                  # the head pipeline of the current branch's MR
glab-pipeline inspect --pipeline-url <url>             # an explicit pipeline
glab-pipeline inspect --mr-url <url>                   # an MR's head pipeline
glab-pipeline inspect --pipeline-id <id> --project group/project --hostname gitlab.example.com
glab-pipeline inspect --logs=all                       # every job's log, not only the failed jobs' logs
glab-pipeline inspect --with-merged-ci-config          # two-step lint that resolves include: against the source branch
glab-pipeline inspect --with-test-report               # the test report even without a failed test job
glab-pipeline inspect --with-downstream-pipelines      # downstream detail for every bridge, not only failed ones
glab-pipeline inspect --with-artefacts                 # download and unpack each job's artifacts archive
glab-pipeline inspect --json                           # the summary as JSON instead of text
glab-pipeline inspect --full                           # ignore what is on disk and fetch everything again
```

- **Name a pipeline by URL** when it is not the current branch's. Outside a checkout, `glab` falls back to its default host. The context flags are all or nothing: `--project` without `--hostname` is an error, not a silent fallback.
- **The dump goes into the pipeline's own dir**, `<base>/<host>/<project>/-/pipelines/<id>/`, the dir `glab-watch pipeline` and `glab-mr-status` also write. The summary's `All files in:` line names it. Take the path from the output, never build it. `--output-dir` writes elsewhere.
- **A rerun is cheap.** An unchanged pipeline costs one request, and a log already on disk is never downloaded again. Run it again after a retry or a push instead of reading an old dump.
- **A dump may already exist.** A `glab-watch pipeline` that saw the pipeline finish wrote the same dump, and its `finished` event carries `summary.txt` in `artifacts`. `glab-mr-status` dumps the head pipeline too. Read that summary first.

## Reading the dump

Read in this order, and stop as soon as you have the cause:

1. **`summary.txt`**, the same text `inspect` prints. It names each failed job with its stage and failure reason, and the file that explains it. Read it first, never the dir listing.
2. **`job-logs/<stage>-<name>-<id>.log`** for a script or runtime failure. The tail carries the error. Read only the logs the summary names, never the logs of jobs that passed.
3. **`test-report.json`** for a test failure. It holds per-test results, which beats grepping a long log.
4. **`lint.json` and `merged.yml`** for a YAML, `needs` or config error. They show what GitLab actually parsed.
5. **`downstream/<bridge>-<id>.json`** for a failed child pipeline. Then inspect the child with `glab-pipeline inspect --pipeline-url <its url>`.

`summary.json` holds the same summary as structured data, for `jq`. `pipeline.json`, `jobs.json` (retried jobs included) and `bridges.json` are the raw GitLab objects.

### Placeholders

Every job has a file in `job-logs/`. A job whose log was not downloaded (it passed, or it is still running, pending, skipped or manual) has a placeholder that starts with `[glab-cli-extras: log not downloaded]`, says why, and gives the command that downloads it, usually `glab-pipeline inspect … --logs=all`. Run that command when you need the log of a job that passed. On GitLab 19.0 and later, a log above 500 KB holds its last 500 KB only and starts with a `[glab-cli-extras: last …]` line.

## Waiting for a pipeline

To wait for a running pipeline, start `glab-watch pipeline <url> --until=finished` as a background task (see the glab-watch skill). Never poll `glab-pipeline inspect` in a loop.

## Actions stay with glab

`glab-pipeline` inspects only. For actions, use `glab` directly:

- `glab ci status --compact` and `glab ci list`: a quick status and the recent pipelines;
- `glab ci run`, `glab ci retry <job>`, `glab ci cancel`: start and manage runs;
- `glab ci lint <file>`: lint a local `.gitlab-ci.yml`;
- `glab job artifact`: download artifacts.
